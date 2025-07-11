import asyncio
import time

import aiodocker
import aiodocker.exceptions
import structlog
from mcstatus import JavaServer

from config import ProxySettings, ServerConfig

logger = structlog.get_logger(__name__)


class DockerManager:
    """Manages interactions with the Docker daemon to control server containers."""

    def __init__(self, docker_url: str):
        try:
            self.client = aiodocker.Docker(url=docker_url)
        except Exception as e:
            logger.error(
                "Failed to connect to Docker daemon. Is Docker running?",
                docker_url=docker_url,
                error=str(e),
            )
            raise

    async def _is_server_ready(
        self, server_config: ServerConfig, max_wait_time: int
    ) -> bool:
        """
        Checks if a Minecraft server inside a container is ready to accept
        connections.
        """
        if server_config.server_type != "java":
            await asyncio.sleep(10)
            return True

        start_time = time.time()
        while time.time() - start_time < max_wait_time:
            try:
                server = await JavaServer.async_lookup(
                    f"127.0.0.1:{server_config.internal_port}"
                )
                await server.async_status()
                logger.info("Server is ready.", server=server_config.name)
                return True
            except Exception:
                await asyncio.sleep(2)
        return False

    async def start_server(
        self, server_config: ServerConfig, settings: ProxySettings
    ) -> bool:
        """Starts a server container and waits for it to become ready."""
        logger.info("Attempting to start server...", server=server_config.name)
        try:
            container = await self.client.containers.get(server_config.container_name)
            await container.start()
            logger.info(
                "Container started, waiting for server...", server=server_config.name
            )

            if not await self._is_server_ready(
                server_config, settings.server_ready_max_wait_time_seconds
            ):
                logger.error(
                    "Server did not become ready in time.", server=server_config.name
                )
                return False
            return True
        except aiodocker.exceptions.DockerError as e:
            logger.error(
                "Docker error while starting server.",
                server=server_config.name,
                status=e.status,
                message=e.message,
            )
            return False
        except Exception as e:
            logger.error(
                "Unexpected error during server startup.",
                server=server_config.name,
                error=str(e),
            )
            return False

    async def stop_server(self, container_name: str) -> bool:
        """Stops a server container."""
        logger.info("Attempting to stop container...", container=container_name)
        try:
            container = await self.client.containers.get(container_name)
            await container.stop()
            logger.info("Container stopped successfully.", container=container_name)
            return True
        except aiodocker.exceptions.DockerError as e:
            logger.error(
                "Docker error while stopping server.",
                container=container_name,
                status=e.status,
                message=e.message,
            )
            return False
        except Exception as e:
            logger.error(
                "Unexpected error during server shutdown.",
                container=container_name,
                error=str(e),
            )
            return False

    async def close(self):
        """Closes the connection to the Docker daemon."""
        await self.client.close()
