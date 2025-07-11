import asyncio
import functools
import time

import docker
import structlog
from mcstatus import BedrockServer, JavaServer

from config import ProxySettings, ServerConfig


class DockerManager:
    """
    Manages Docker containers for Minecraft servers,
    providing methods to start, stop, and check server status.
    """

    def __init__(self):
        self.logger = structlog.get_logger(__name__)
        self.client = None
        self._connect_to_docker()

    def _connect_to_docker(self):
        """Attempts to connect to the Docker daemon."""
        try:
            self.client = docker.from_env()
            self.client.ping()
            self.logger.info("Successfully connected to Docker daemon.")
        except docker.errors.DockerException as e:
            self.logger.critical(f"Could not connect to Docker daemon: {e}")
            raise

    async def _run_blocking_docker_call(self, func, *args, **kwargs):
        """Helper to run blocking Docker API calls in a thread pool."""
        loop = asyncio.get_running_loop()
        # Use functools.partial to pass method and its arguments
        return await loop.run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )

    async def is_container_running(self, container_name: str) -> bool:
        """Asynchronously checks if a Docker container is running."""
        try:
            container = await self._run_blocking_docker_call(
                self.client.containers.get, container_name
            )
            # Accessing container.status is also a blocking operation
            status = await self._run_blocking_docker_call(lambda c: c.status, container)
            self.logger.debug(
                "Container status check",
                container_name=container_name,
                status=status,
            )
            return status == "running"
        except docker.errors.NotFound:
            self.logger.debug(
                "Container not found, assuming not running.",
                container_name=container_name,
            )
            return False
        except Exception as e:
            self.logger.error(
                "Error checking container status asynchronously.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return False

    async def start_server(
        self, server_config: ServerConfig, settings: ProxySettings
    ) -> bool:
        """
        Asynchronously starts a Docker container and waits for the Minecraft server
        to be ready.
        """
        container_name = server_config.container_name
        self.logger.info(
            "Attempting to start Minecraft server container asynchronously...",
            container_name=container_name,
        )
        try:
            container = await self._run_blocking_docker_call(
                self.client.containers.get, container_name
            )
            await self._run_blocking_docker_call(container.start)
            self.logger.info(
                "Docker 'start' command issued. Waiting for network to settle.",
                container_name=container_name,
                delay_seconds=settings.server_startup_delay_seconds,
            )
            # Use asyncio.sleep instead of time.sleep
            await asyncio.sleep(settings.server_startup_delay_seconds)

            return await self.wait_for_server_query_ready(
                server_config,
                settings.server_ready_max_wait_time_seconds,
                settings.query_timeout_seconds,
            )
        except Exception as e:
            self.logger.error(
                "Error during async server startup.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return False

    async def stop_server(self, container_name: str) -> bool:
        """Asynchronously stops a Docker container."""
        try:
            container = await self._run_blocking_docker_call(
                self.client.containers.get, container_name
            )
            status = await self._run_blocking_docker_call(lambda c: c.status, container)
            if status == "running":
                self.logger.info(
                    "Attempting to stop Minecraft server container asynchronously...",
                    container_name=container_name,
                )
                await self._run_blocking_docker_call(container.stop)
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
        except Exception as e:
            self.logger.error(
                "Error during async server stop.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return False

    async def wait_for_server_query_ready(
        self,
        server_config: ServerConfig,
        max_wait_seconds: int,
        query_timeout_seconds: int,
    ) -> bool:
        """
        Asynchronously pings the server (Java or Bedrock) until it responds or
        timeout is reached.
        """
        self.logger.info(
            "Waiting for server to be queryable.",
            container_name=server_config.container_name,
            max_wait=f"{max_wait_seconds}s",
            query_timeout=f"{query_timeout_seconds}s",
        )
        start_time = time.time()
        while time.time() - start_time < max_wait_seconds:
            try:
                if server_config.server_type == "java":
                    status = await JavaServer.async_lookup(
                        f"{server_config.container_name}:{server_config.internal_port}",
                        timeout=query_timeout_seconds,
                    ).async_status()
                elif server_config.server_type == "bedrock":
                    status = await BedrockServer.async_lookup(
                        f"{server_config.container_name}:{server_config.internal_port}",
                        timeout=query_timeout_seconds,
                    ).async_status()
                else:
                    self.logger.error(
                        "Unknown server type.", server_type=server_config.server_type
                    )
                    return False

                if status:
                    self.logger.info(
                        "Server is ready and queryable.",
                        container_name=server_config.container_name,
                        ping=status.latency if hasattr(status, "latency") else "N/A",
                    )
                    return True
            except Exception as e:
                self.logger.debug(
                    "Server not yet ready or query failed.",
                    container_name=server_config.container_name,
                    error=str(e),
                )
            await asyncio.sleep(1)  # Check every second

        self.logger.warning(
            "Server did not become ready within the allotted time.",
            container_name=server_config.container_name,
        )
        return False
