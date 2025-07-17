# docker_manager.py
"""
Manages Docker containers for Minecraft servers using asynchronous operations.
"""

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
    containers for the game servers.
    """

    def __init__(self, app_config: AppConfig):
        self.app_config = app_config
        self.docker = aiodocker.Docker()
        log.info("DockerManager initialized")

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
            container = await self.docker.containers.get(container_name)
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
        Asynchronously checks if a container is running.
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
        Asynchronously starts a Docker container and waits for it to be queryable.
        Returns True on success, False on failure.
        """
        container_name = server_config.container_name
        log.info("Starting container", container_name=container_name)
        async with self.get_container(container_name) as container:
            if not container:
                return False

            try:
                await container.start()
                log.info(
                    "Container started successfully", container_name=container_name
                )
                # Reintroduce delay before querying to match original logic.
                await asyncio.sleep(self.app_config.server_startup_delay)
                return await self.wait_for_server_query_ready(server_config)
            except aiodocker.exceptions.DockerError as e:
                if "already started" in str(e).lower():
                    log.warning(
                        "Container is already running",
                        container_name=container_name,
                    )
                    return True
                else:
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
                        "Container stopped successfully", container_name=container_name
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
        Asynchronously waits for a Minecraft server to become queryable.
        Returns True if ready, False if it times out.
        """
        start_time = time.time()
        wait_timeout = timeout or self.app_config.server_startup_timeout
        query_timeout = self.app_config.query_timeout
        log.info(
            "Waiting for server to be queryable",
            container_name=server_config.container_name,
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
                    # BedrockServer.lookup is synchronous, run in a thread pool
                    server = await asyncio.to_thread(
                        BedrockServer.lookup, lookup_str, timeout=query_timeout
                    )

                # BedrockServer.lookup returns a BedrockServer object which
                # then requires an async_status() call.
                await server.async_status()
                log.info(
                    "Server is queryable!",
                    container_name=server_config.container_name,
                )
                return True
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                log.debug(
                    "Server not queryable yet, retrying...",
                    container_name=server_config.container_name,
                    error=str(e),
                )
                # CORRECTED: Use query_timeout for sleep interval to match
                # original behavior.
                await asyncio.sleep(query_timeout)

        log.error(
            "Timeout waiting for server to be queryable",
            container_name=server_config.container_name,
        )
        return False

    async def close(self):
        """Closes the asynchronous aiodocker session."""
        await self.docker.close()
        log.info("DockerManager session closed")
