import asyncio
import sys
import time

import docker
import structlog
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

    def is_container_running(self, container_name: str) -> bool:
        """Checks if a Docker container's status is 'running'."""
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

    def start_server(
        self, server_config: ServerConfig, settings: ProxySettings
    ) -> bool:
        """
        Starts a given Minecraft server container and waits for it to become ready.
        """
        container_name = server_config.container_name
        self.logger.info(
            "Attempting to start Minecraft server container...",
            container_name=container_name,
        )
        try:
            container = self.client.containers.get(container_name)
            container.start()
            self.logger.info(
                "Docker 'start' command issued. Waiting for network to settle.",
                container_name=container_name,
                delay_seconds=settings.server_startup_delay_seconds,
            )
            time.sleep(settings.server_startup_delay_seconds)

            # Execute the async wait_for_server_query_ready using asyncio.run()
            return asyncio.run(
                self.wait_for_server_query_ready(
                    server_config,
                    settings.server_ready_max_wait_time_seconds,
                    settings.query_timeout_seconds,
                )
            )
        except docker.errors.NotFound:
            self.logger.error(
                "Docker container not found. Cannot start.",
                container_name=container_name,
            )
            return False
        except docker.errors.APIError as e:
            self.logger.error(
                "Docker API error during start.",
                container_name=container_name,
                error=str(e),
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error during server startup.",
                container_name=container_name,
                error=str(e),
            )
            return False

    def stop_server(self, container_name: str) -> bool:
        """
        Stops a given Minecraft server container.
        """
        try:
            container = self.client.containers.get(container_name)
            if container.status == "running":
                self.logger.info(
                    "Attempting to stop Minecraft server container...",
                    container_name=container_name,
                )
                container.stop()
                self.logger.info(
                    "Server stopped successfully.", container_name=container_name
                )
            return True
        except docker.errors.NotFound:
            self.logger.debug(
                "Container not found, assuming already stopped.",
                container_name=container_name,
            )
            return True
        except docker.errors.APIError as e:
            self.logger.error(
                "Docker API error during stop.",
                container_name=container_name,
                error=str(e),
            )
            return False
        except Exception as e:
            self.logger.error(
                "Unexpected error during server stop.",
                container_name=container_name,
                error=str(e),
            )
            return False

    async def wait_for_server_query_ready(
        self,
        server_config: ServerConfig,
        max_wait_seconds: int,
        query_timeout_seconds: int,
    ) -> bool:
        """
        Polls a Minecraft server using mcstatus until it responds or a timeout
        is reached. This is an asynchronous function.
        """
        container_name = server_config.container_name
        target_ip = container_name
        target_port = server_config.internal_port
        server_type = server_config.server_type

        self.logger.info(
            "Waiting for server to respond to query",
            container_name=container_name,
            target=f"{target_ip}:{target_port}",
            max_wait_seconds=max_wait_seconds,
        )
        start_time = time.time()

        while time.time() - start_time < max_wait_seconds:
            try:
                status = None
                if server_type == "bedrock":
                    server = BedrockServer.lookup(
                        f"{target_ip}:{target_port}",
                        timeout=query_timeout_seconds,
                    )
                    status = server.status()
                elif server_type == "java":
                    server = JavaServer.lookup(
                        f"{target_ip}:{target_port}",
                        timeout=query_timeout_seconds,
                    )
                    status = server.status()

                if status:
                    self.logger.info(
                        "Server responded to query. Ready!",
                        container_name=container_name,
                        latency_ms=status.latency,
                    )
                    return True
            except Exception as e:
                self.logger.debug(
                    "Query failed, retrying...",
                    container_name=container_name,
                    error=str(e),
                )
            await asyncio.sleep(query_timeout_seconds)

        self.logger.error(
            f"Timeout: Server did not respond after {max_wait_seconds} seconds. "
            "Proceeding anyway.",
            container_name=container_name,
        )
        return False
