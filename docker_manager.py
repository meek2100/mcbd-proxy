# docker_manager.py
import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import aiodocker
import structlog
from aiodocker.containers import DockerContainer
from mcstatus import BedrockServer, JavaServer

from config import AppConfig, GameServerConfig

log = structlog.get_logger()


class DockerManager:
    """
    An asynchronous manager for starting, stopping, and inspecting Docker
    containers for the game servers. It encapsulates all interactions with
    the Docker daemon using the aiodocker library.
    """

    def __init__(self, app_config: AppConfig):
        self.app_config = app_config
        self.docker: Optional[aiodocker.Docker] = None
        self._lock = asyncio.Lock()
        log.info("DockerManager initialized (client will connect on first use).")

    async def _get_client(self) -> aiodocker.Docker:
        """
        Lazily initializes and returns the aiodocker client, ensuring it
        happens only once and in an async context. This is more resilient
        than connecting in the constructor.
        """
        async with self._lock:
            if self.docker is None:
                log.info("First use: initializing Docker client...")
                try:
                    self.docker = aiodocker.Docker()
                    # Verify the connection is alive with a lightweight command
                    await self.docker.version()
                    log.info("Docker client connected successfully.")
                except Exception:
                    log.critical(
                        "Failed to initialize Docker client. Is Docker running?",
                        exc_info=True,
                    )
                    # Ensure we close a partially opened client
                    if self.docker:
                        await self.docker.close()
                    self.docker = None  # Reset to allow retries
                    raise
            return self.docker

    @asynccontextmanager
    async def get_container(
        self, container_name: str
    ) -> AsyncGenerator[Optional[DockerContainer], None]:
        """
        An async context manager to safely get a container object.
        Handles exceptions gracefully if the container is not found.
        """
        container = None
        try:
            docker = await self._get_client()
            container = await docker.containers.get(container_name)
            yield container
        except aiodocker.exceptions.DockerError as e:
            if e.status == 404:
                log.debug("Container not found.", container_name=container_name)
                yield None
            else:
                log.error(
                    "API error getting container",
                    container_name=container_name,
                    status=e.status,
                    exc_info=True,
                )
                yield None
        except Exception:
            log.error(
                "Unexpected error in get_container",
                container_name=container_name,
                exc_info=True,
            )
            yield None

    async def is_container_running(self, container_name: str) -> bool:
        """
        Asynchronously checks if a container's state is 'running'.
        Returns False if the container doesn't exist or an API error occurs.
        """
        log.debug("Checking container status", container_name=container_name)
        async with self.get_container(container_name) as container:
            if not container:
                return False
            try:
                container_info = await container.show()
                is_running = container_info.get("State", {}).get("Running", False)
                log.debug(
                    "Container state",
                    container_name=container_name,
                    is_running=is_running,
                )
                return is_running
            except aiodocker.exceptions.DockerError:
                log.error(
                    "Could not get container info",
                    container_name=container_name,
                    exc_info=True,
                )
        return False

    async def start_server(self, server_config: GameServerConfig) -> bool:
        """
        Asynchronously starts a Docker container and waits for the game
        server within it to become queryable.

        Returns True on success, False on failure.
        """
        container_name = server_config.container_name
        log.info("Attempting to start container", container_name=container_name)
        async with self.get_container(container_name) as container:
            if not container:
                return False

            try:
                await container.start()
                log.info(
                    "Container started successfully", container_name=container_name
                )
                await asyncio.sleep(self.app_config.server_startup_delay)
                return await self.wait_for_server_query_ready(server_config)
            except aiodocker.exceptions.DockerError as e:
                if "already started" in str(e).lower():
                    log.warning(
                        "Container is already running", container_name=container_name
                    )
                    return True
                log.error(
                    "Failed to start container",
                    container_name=container_name,
                    exc_info=True,
                )
                return False

    async def stop_server(self, container_name: str, stop_timeout: int):
        """
        Asynchronously stops a Docker container with a specified timeout.
        """
        log.info(
            "Stopping container",
            container_name=container_name,
            timeout=stop_timeout,
        )
        async with self.get_container(container_name) as container:
            if container:
                try:
                    await container.stop(t=stop_timeout)
                    log.info(
                        "Container stopped successfully",
                        container_name=container_name,
                    )
                except aiodocker.exceptions.DockerError:
                    log.error(
                        "Failed to stop container",
                        container_name=container_name,
                        exc_info=True,
                    )

    async def wait_for_server_query_ready(
        self, server_config: GameServerConfig, timeout: Optional[int] = None
    ) -> bool:
        """
        Asynchronously polls a Minecraft server using mcstatus until it
        responds to a status query or a timeout is reached.

        Returns True if the server becomes ready, False if it times out.
        """
        start_time = time.time()
        wait_timeout = timeout or self.app_config.server_startup_timeout
        query_timeout = self.app_config.query_timeout
        log.info(
            "Waiting for server to be queryable",
            server=server_config.name,
            timeout=wait_timeout,
        )

        while time.time() - start_time < wait_timeout:
            try:
                lookup_str = f"{server_config.host}:{server_config.query_port}"
                if server_config.game_type == "java":
                    server = await JavaServer.async_lookup(
                        lookup_str, timeout=query_timeout
                    )
                else:
                    server = await asyncio.to_thread(
                        BedrockServer.lookup, lookup_str, timeout=query_timeout
                    )
                await server.async_status()
                log.info("Server is queryable!", server=server_config.name)
                return True
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                log.debug(
                    "Server not queryable yet, retrying...",
                    server=server_config.name,
                    error=str(e),
                )
                await asyncio.sleep(query_timeout)

        log.error(
            "Timeout waiting for server to be queryable",
            server=server_config.name,
        )
        return False

    async def close(self):
        """Closes the asynchronous aiodocker session if it was created."""
        if self.docker:
            await self.docker.close()
            log.info("DockerManager session closed")
