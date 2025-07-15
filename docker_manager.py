# docker_manager.py
"""
Manages Docker containers for Minecraft servers, now with asynchronous operations.
"""

import asyncio
import time
from contextlib import asynccontextmanager

import aiodocker
import structlog
from mcstatus import BedrockServer, JavaServer

from config import AppConfig, GameServerConfig

log = structlog.get_logger()


class DockerManager:
    """
    An asynchronous manager for starting, stopping, and inspecting Docker
    containers.
    """

    def __init__(self, app_config: AppConfig):
        self.app_config = app_config
        self.docker = aiodocker.Docker()
        log.info("DockerManager initialized")

    @asynccontextmanager
    async def get_container(self, container_name: str):
        """
        An async context manager to safely get a container object.
        Handles exceptions if the container is not found.
        """
        container = None
        try:
            container = await self.docker.containers.get(container_name)
            yield container
        except aiodocker.exceptions.DockerError as e:
            if e.status == 404:
                yield None
            else:
                log.error(
                    "Docker error on get_container",
                    container_name=container_name,
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
        Returns True if the container is running, False otherwise.
        """
        log.debug("Checking container status", container_name=container_name)
        async with self.get_container(container_name) as container:
            if container:
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

    async def start_server(self, server_config: GameServerConfig):
        """
        Asynchronously starts a Docker container for a game server.
        """
        container_name = server_config.container_name
        log.info("Starting container", container_name=container_name)
        async with self.get_container(container_name) as container:
            if container:
                try:
                    await container.start()
                    log.info(
                        "Container started successfully", container_name=container_name
                    )
                    await self.wait_for_server_query_ready(server_config)
                except aiodocker.exceptions.DockerError as e:
                    if "already started" in str(e).lower():
                        log.warning(
                            "Container is already running",
                            container_name=container_name,
                        )
                    else:
                        log.error(
                            "Failed to start container",
                            container_name=container_name,
                            exc_info=True,
                        )
            else:
                log.error(
                    "Container not found, cannot start", container_name=container_name
                )

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

    async def wait_for_server_query_ready(self, server_config: GameServerConfig):
        """
        Asynchronously waits for a Minecraft server to become queryable.
        """
        start_time = time.time()
        log.info(
            "Waiting for server to be queryable",
            container_name=server_config.container_name,
            timeout=self.app_config.server_startup_timeout,
        )

        while True:
            if time.time() - start_time > self.app_config.server_startup_timeout:
                log.error(
                    "Timeout waiting for server to be queryable",
                    container_name=server_config.container_name,
                )
                return

            try:
                if server_config.game_type == "java":
                    server = await JavaServer.async_lookup(
                        f"{server_config.host}:{server_config.query_port}"
                    )
                else:  # bedrock
                    # BedrockServer does not have async_lookup, instantiate directly
                    server = BedrockServer(server_config.host, server_config.query_port)

                await server.async_status()
                log.info(
                    "Server is queryable!", container_name=server_config.container_name
                )
                return
            except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
                log.debug(
                    "Server not queryable yet, retrying...",
                    container_name=server_config.container_name,
                    error=str(e),
                )
                await asyncio.sleep(self.app_config.server_check_interval)

    async def close(self):
        """Closes the aiodocker session."""
        await self.docker.close()
        log.info("DockerManager session closed")
