# docker_manager.py

import asyncio
import time

import aiodocker
import aiodocker.exceptions
import structlog
from mcstatus import BedrockServer, JavaServer

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

    async def is_container_running(self, container_name: str) -> bool:
        """Checks if a given Docker container is currently running."""
        try:
            container = await self.client.containers.get(container_name)
            info = await container.show()
            return info["State"]["Running"]
        except aiodocker.exceptions.DockerError as e:
            if e.status == 404:
                # Container not found, so it's definitely not running
                return False
            logger.error(
                "Docker error checking container status.",
                container=container_name,
                status=e.status,
                message=e.message,
            )
            raise  # Re-raise unexpected Docker errors
        except Exception as e:
            logger.error(
                "An unexpected error occurred while checking container status.",
                container=container_name,
                error=str(e),
            )
            raise

    async def wait_for_server_query_ready(
        self,
        server_config: ServerConfig,
        max_wait_time: int,
        query_timeout: int,
    ) -> bool:
        """
        Polls a Minecraft server using mcstatus until it responds or a timeout
        is reached, confirming it's ready for traffic.
        """
        start_time = time.time()
        server_address = f"127.0.0.1:{server_config.internal_port}"

        ServerClass = None
        if server_config.server_type == "java":
            ServerClass = JavaServer
        elif server_config.server_type == "bedrock":
            ServerClass = BedrockServer
        else:
            logger.warning(
                "Unsupported server type for readiness check",
                server_type=server_config.server_type,
            )
            await asyncio.sleep(5)  # Wait a bit even for unsupported types
            return False

        while time.time() - start_time < max_wait_time:
            try:
                server = await ServerClass.async_lookup(
                    server_address, timeout=query_timeout
                )
                await server.async_status()
                logger.info("Server is ready.", server=server_config.name)
                return True
            except Exception as e:
                logger.debug(
                    "Server not ready yet, retrying...",
                    server=server_config.name,
                    error=str(e),
                )
                # This sleep should use the query_timeout to match test expectations
                await asyncio.sleep(query_timeout)  # FIX: Use query_timeout here
        logger.error(
            "Server did not become query-ready in time.", server=server_config.name
        )
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

            if not await self.wait_for_server_query_ready(
                server_config,
                settings.server_ready_max_wait_time_seconds,
                settings.query_timeout_seconds,
            ):
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
                "An unexpected error occurred during server startup.",
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
                "An unexpected error occurred during server shutdown.",
                container=container_name,
                error=str(e),
            )
            return False

    async def close(self):
        """Closes the connection to the Docker daemon."""
        await self.client.close()
