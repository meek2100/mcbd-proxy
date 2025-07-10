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
                "FATAL: Could not connect to Docker daemon.", error=str(e)
            )
            sys.exit(1)

    def is_container_running(self, container_name: str) -> bool:
        """Checks if a Docker container's status is 'running'."""
        try:
            container = self.client.containers.get(container_name)
            return container.status == "running"
        except docker.errors.NotFound:
            return False
        except docker.errors.APIError as e:
            self.logger.error("API error checking container status.", error=str(e))
            return False
        return False

    def start_server(
        self, server_config: ServerConfig, settings: ProxySettings
    ) -> bool:
        """Starts a server and waits for it to become ready."""
        container_name = server_config.container_name
        self.logger.info("Starting container...", container_name=container_name)
        try:
            container = self.client.containers.get(container_name)
            container.start()
            self.logger.info("Container started.", container_name=container_name)
            time.sleep(settings.server_startup_delay_seconds)
            return self.wait_for_server_query_ready(
                server_config,
                settings.server_ready_max_wait_time_seconds,
                settings.query_timeout_seconds,
            )
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            self.logger.error("Error starting container.", error=str(e))
            return False

    def stop_server(self, container_name: str) -> bool:
        """Stops a given Minecraft server container."""
        try:
            container = self.client.containers.get(container_name)
            if container.status == "running":
                self.logger.info("Stopping container.", container_name=container_name)
                container.stop()
                self.logger.info("Container stopped.", container_name=container_name)
            return True
        except (docker.errors.NotFound, docker.errors.APIError) as e:
            self.logger.error("Error stopping container.", error=str(e))
            return False

    def wait_for_server_query_ready(
        self,
        server_config: ServerConfig,
        max_wait_seconds: int,
        query_timeout_seconds: int,
    ) -> bool:
        """
        Polls a Minecraft server until it responds for several consecutive
        attempts, ensuring it's stable and fully loaded.
        """
        self.logger.info(
            "Waiting for server to become stable...",
            container=server_config.container_name,
            timeout=max_wait_seconds,
        )
        start_time = time.time()
        consecutive_successes = 0
        required_successes = 3  # Require 3 successful pings in a row

        while time.time() - start_time < max_wait_seconds:
            if not self.is_container_running(server_config.container_name):
                self.logger.error(
                    "Container stopped unexpectedly while waiting for ready."
                )
                return False
            try:
                if server_config.server_type == "bedrock":
                    server = BedrockServer.lookup(
                        f"{server_config.container_name}:{server_config.internal_port}",
                        timeout=query_timeout_seconds,
                    )
                else:  # java
                    server = JavaServer.lookup(
                        f"{server_config.container_name}:{server_config.internal_port}",
                        timeout=query_timeout_seconds,
                    )
                status = server.status()
                if status:
                    consecutive_successes += 1
                    self.logger.debug(
                        "Successful ping.",
                        successes=f"{consecutive_successes}/{required_successes}",
                    )
                    if consecutive_successes >= required_successes:
                        self.logger.info("Server is stable and ready.")
                        return True
                else:
                    consecutive_successes = 0
            except Exception as e:
                self.logger.debug("Query failed, retrying...", error=str(e))
                consecutive_successes = 0
            time.sleep(2)

        self.logger.error("Timeout: Server did not become stable.")
        return False
