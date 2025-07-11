# docker_manager.py
import asyncio

import docker
import structlog
from docker.errors import APIError, NotFound
from mcstatus import BedrockServer, JavaServer

from config import ProxySettings, ServerConfig


class DockerManager:
    """Handles all direct interactions with the Docker daemon asynchronously."""

    def __init__(self):
        self.logger = structlog.get_logger(__name__)
        self.client = None

    async def connect(self):
        """Connects to the Docker daemon and verifies the connection."""
        try:
            self.client = await asyncio.to_thread(docker.from_env)
            await asyncio.to_thread(self.client.ping)
            self.logger.info("Successfully connected to the Docker daemon.")
        except APIError as e:
            self.logger.critical(
                "Failed to connect to Docker daemon. Is it running?", error=str(e)
            )
            raise

    async def start_server(
        self, server_config: ServerConfig, settings: ProxySettings
    ) -> bool:
        """Starts a server container and waits for it to become ready."""
        container_name = server_config.container_name
        self.logger.info("Attempting to start server", server=container_name)

        try:
            container = await asyncio.to_thread(
                self.client.containers.get, container_name
            )
            if container.status != "running":
                self.logger.info(
                    "Container found but is not running. Starting it...",
                    server=container_name,
                )
                await asyncio.to_thread(container.start)
            else:
                self.logger.info("Container is already running.", server=container_name)
        except NotFound:
            self.logger.error(
                "Cannot start server: container not found.", server=container_name
            )
            return False
        except APIError as e:
            self.logger.error(
                "Docker API error while starting server.",
                server=container_name,
                error=str(e),
            )
            return False

        return await self._wait_for_server_ready(server_config, settings)

    async def stop_server(self, container_name: str) -> bool:
        """Stops a running server container."""
        self.logger.info("Attempting to stop server", server=container_name)
        try:
            container = await asyncio.to_thread(
                self.client.containers.get, container_name
            )
            if container.status == "running":
                self.logger.info("Stopping container...", server=container_name)
                await asyncio.to_thread(container.stop, timeout=15)
                self.logger.info(
                    "Container stopped successfully.", server=container_name
                )
            return True
        except NotFound:
            self.logger.warning(
                "Attempted to stop a container that was not found.",
                server=container_name,
            )
            return True  # If it doesn't exist, it's stopped.
        except APIError as e:
            self.logger.error(
                "Docker API error while stopping server.",
                server=container_name,
                error=str(e),
            )
            return False

    async def _wait_for_server_ready(
        self, server_config: ServerConfig, settings: ProxySettings
    ) -> bool:
        """Probes a server until it responds to a status query."""
        self.logger.info("Waiting for server to become query-ready...")
        lookup_addr = f"127.0.0.1:{server_config.internal_port}"
        ServerClass = (
            JavaServer if server_config.server_type == "java" else BedrockServer
        )

        for attempt in range(settings.server_ready_max_wait_time_seconds):
            try:
                server = await asyncio.to_thread(ServerClass.lookup, lookup_addr)
                await server.async_status()
                self.logger.info(
                    "Server is now query-ready.", server=server_config.name
                )
                return True
            except Exception:
                if attempt == settings.server_ready_max_wait_time_seconds - 1:
                    break
                await asyncio.sleep(1)

        self.logger.error(
            "Timed out waiting for server to become ready.",
            server=server_config.name,
        )
        return False
