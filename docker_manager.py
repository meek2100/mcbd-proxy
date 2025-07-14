# docker_manager.py
import aiodocker
import structlog
from aiodocker.exceptions import DockerError


class DockerManager:
    """
    Manages interactions with the Docker daemon, such as starting, stopping,
    and inspecting containers asynchronously.
    """

    def __init__(self, docker_url: str):
        self.logger = structlog.get_logger(__name__)
        try:
            # Use the provided URL or default to the environment/standard socket
            self.client = aiodocker.Docker(url=docker_url if docker_url else None)
        except Exception as e:
            # This catch is for issues during client instantiation,
            # like a malformed URL.
            self.logger.error(
                "Failed to initialize Docker client. Check DOCKER_URL.",
                docker_url=docker_url,
                error=str(e),
            )
            # Propagate the error to be handled by the application's entry point.
            raise

    async def close(self):
        """Closes the connection to the Docker daemon."""
        await self.client.close()
        self.logger.info("Docker client session closed.")

    async def is_container_running(self, container_name: str) -> bool:
        """Checks if the specified container is currently running."""
        try:
            container = await self.client.containers.get(container_name)
            info = await container.show()
            return info.get("State", {}).get("Running", False)
        except DockerError as e:
            if e.status == 404:
                return False  # Container not found means it's not running
            self.logger.error(
                "Error checking container status.",
                container_name=container_name,
                error=e.message,
            )
            return False

    async def stop_server(self, container_name: str, timeout: int = 10):
        """Stops the specified container if it is running."""
        try:
            container = await self.client.containers.get(container_name)
            info = await container.show()

            if info.get("State", {}).get("Running"):
                self.logger.info("Stopping container.", container_name=container_name)
                await container.stop(timeout=timeout)
            else:
                self.logger.info(
                    "Container already stopped.", container_name=container_name
                )
        except DockerError as e:
            self.logger.error(
                "Failed to stop container.",
                container_name=container_name,
                error=e.message,
            )

    async def ensure_server_running(self, container_name: str) -> str | None:
        """
        Ensures the specified container is running. If it's not, this method
        will attempt to start it.

        Returns the IP address of the container if it's running,
        otherwise None.
        """
        try:
            container = await self.client.containers.get(container_name)
            container_info = await container.show()
            state = container_info.get("State", {})

            if state.get("Running"):
                self.logger.debug(
                    "Container is already running.", container_name=container_name
                )
            else:
                self.logger.info(
                    "Container is not running. Attempting to start...",
                    container_name=container_name,
                )
                await container.start()
                # Refresh info after starting
                container_info = await container.show()

            # Extract the IP address from the network settings
            networks = container_info.get("NetworkSettings", {}).get("Networks", {})
            if networks:
                # Get the IP from the first available network
                first_network = next(iter(networks.values()), None)
                if first_network and "IPAddress" in first_network:
                    ip_address = first_network["IPAddress"]
                    if ip_address:
                        return ip_address

            self.logger.error(
                "Could not determine IP address for running container.",
                container_name=container_name,
            )
            return None

        except DockerError as e:
            if e.status == 404:
                self.logger.error("Container not found.", container_name=container_name)
            else:
                self.logger.error(
                    "An error occurred interacting with Docker.",
                    container_name=container_name,
                    status_code=e.status,
                    message=e.message,
                )
            return None
        except Exception as e:
            self.logger.error(
                "An unexpected error occurred in DockerManager.",
                container_name=container_name,
                error=str(e),
                exc_info=True,
            )
            return None
