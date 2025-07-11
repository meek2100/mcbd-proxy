# docker_manager.py
import asyncio
import sys
import time

import docker
import structlog
from docker.errors import APIError, NotFound
from mcstatus import BedrockServer, JavaServer

from config import ProxySettings, ServerConfig


class DockerManager:
    """Handles all direct interactions with the Docker daemon."""

    def __init__(self):
        """Initializes the DockerManager."""
        self.logger = structlog.get_logger(__name__)
        self.client = None

    def connect(self):
        """Connects to the Docker daemon via the mounted socket."""
        try:
            self.client = docker.from_env()
            self.client.ping()
            self.logger.info("Successfully connected to the Docker daemon.")
        except Exception as e:
            self.logger.critical(
                "FATAL: Could not connect to Docker daemon. "
                "Is /var/run/docker.sock mounted?",
                error=str(e),
            )
            sys.exit(1)

    async def _is_java_server_ready(
        self, host: str, port: int, query_timeout_seconds: int
    ) -> bool:
        """
        Helper async function to check if a Java Minecraft server is ready.
        Uses asyncio.to_thread for the blocking mcstatus lookup.
        """
        try:
            # JavaServer.lookup can take a timeout directly
            await asyncio.to_thread(
                lambda: JavaServer.lookup(f"{host}:{port}").status(
                    query_timeout_seconds
                )
            )
            return True
        except Exception as e:
            self.logger.debug(
                "Java server query failed.", host=host, port=port, error=str(e)
            )
            return False

    async def _is_bedrock_server_ready(
        self, host: str, port: int, query_timeout_seconds: int
    ) -> bool:
        """
        Helper async function to check if a Bedrock Minecraft server is ready.
        Uses asyncio.to_thread for the blocking mcstatus lookup.
        """
        try:
            # BedrockServer.lookup can take a timeout directly
            await asyncio.to_thread(
                lambda: BedrockServer.lookup(f"{host}:{port}").status(
                    query_timeout_seconds
                )
            )
            return True
        except Exception as e:
            self.logger.debug(
                "Bedrock server query failed.", host=host, port=port, error=str(e)
            )
            return False

    async def wait_for_server_query_ready(
        self,
        server_config: ServerConfig,
        settings: ProxySettings,
        polling_interval_seconds: float = 0.5,
    ) -> bool:
        """
        Asynchronously waits for the Minecraft server inside the container to be ready
        to respond to queries.
        """
        # Use container_name for internal Docker network communication
        target_ip = server_config.container_name
        target_port = server_config.internal_port
        server_type = server_config.server_type
        max_wait_seconds = settings.server_ready_max_wait_time_seconds
        query_timeout_seconds = settings.query_timeout_seconds

        self.logger.info(
            "Waiting for server query ready",
            container_name=server_config.container_name,
            host=target_ip,
            port=target_port,
            server_type=server_type,
            max_wait_seconds=max_wait_seconds,
        )

        start_time = time.time()
        while (time.time() - start_time) < max_wait_seconds:
            is_ready = False
            if server_type == "java":
                is_ready = await self._is_java_server_ready(
                    target_ip, target_port, query_timeout_seconds
                )
            elif server_type == "bedrock":
                is_ready = await self._is_bedrock_server_ready(
                    target_ip, target_port, query_timeout_seconds
                )
            else:
                self.logger.warning(
                    "Unsupported server type for readiness check",
                    server_type=server_type,
                )
                return False

            if is_ready:
                self.logger.info(
                    "Server is query ready", container_name=server_config.container_name
                )
                return True

            await asyncio.sleep(polling_interval_seconds)

        self.logger.warning(
            f"Server query readiness timed out after {max_wait_seconds} seconds. "
            "Proceeding anyway.",
            container_name=server_config.container_name,
        )
        return False

    async def start_server(
        self, server_config: ServerConfig, settings: ProxySettings
    ) -> bool:
        """
        Asynchronously starts a Minecraft server container if not already running.
        Also waits for the server to be query ready.
        """
        container_name = server_config.container_name
        self.logger.info("Attempting to start server", container_name=container_name)
        try:
            container = await asyncio.to_thread(
                lambda: self.client.containers.get(container_name)
            )
            if container.status == "running":
                self.logger.info(
                    "Server already running.", container_name=container_name
                )
                # If already running, we still need to wait for it to be query ready
                # just in case (e.g., proxy restarted but server didn't stop).
                return await self.wait_for_server_query_ready(server_config, settings)
            else:
                self.logger.info(
                    "Container found but not running. Attempting to start.",
                    container_name=container_name,
                    status=container.status,
                )
                await asyncio.to_thread(container.start)
                self.logger.info("Container started.", container_name=container_name)
        except NotFound:
            self.logger.error(
                "Docker container not found. Cannot start.",
                container_name=container_name,
            )
            return False  # Or handle creation if that's desired behavior
        except APIError as e:
            self.logger.error(
                "Docker API error during start.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error during server startup.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return False

        # After ensuring container is running, wait for the server inside to be
        # query ready
        return await self.wait_for_server_query_ready(server_config, settings)

    async def stop_server(self, container_name: str) -> bool:
        """
        Asynchronously stops a Minecraft server container.
        """
        self.logger.info("Attempting to stop server", container_name=container_name)
        try:
            container = await asyncio.to_thread(
                lambda: self.client.containers.get(container_name)
            )
            if container.status == "running":
                self.logger.info("Stopping container...", container_name=container_name)
                await asyncio.to_thread(container.stop)
                self.logger.info(
                    "Server stopped successfully.", container_name=container_name
                )
            else:
                self.logger.debug(
                    "Container not running, assuming already stopped.",
                    container_name=container_name,
                    status=container.status,
                )
            return True
        except NotFound:
            self.logger.debug(
                "Container not found, assuming already stopped.",
                container_name=container_name,
            )
            return True
        except APIError as e:
            self.logger.error(
                "Docker API error during stop.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error during server stop.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return False

    def is_container_running(self, container_name: str) -> bool:
        """
        Checks if a Docker container's status is 'running'.
        This method remains synchronous, intended for calls from non-async
        contexts (e.g., the monitor thread).
        """
        try:
            container = self.client.containers.get(container_name)
            return container.status == "running"
        except docker.errors.NotFound:
            self.logger.debug(
                "Container not found, assuming not running.",
                container_name=container_name,
            )
            return False
        except docker.errors.APIError as e:
            self.logger.error(
                "API error checking container status.",
                container_name=container_name,
                error=str(e),
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error checking container status.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return False
