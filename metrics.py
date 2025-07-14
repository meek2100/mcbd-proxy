# metrics.py
"""
Manages Prometheus metrics for the proxy.
"""

import asyncio

import structlog
from prometheus_client import Gauge, start_http_server

from config import AppConfig
from docker_manager import DockerManager

log = structlog.get_logger()

# Define Prometheus metrics
active_connections_gauge = Gauge(
    "active_connections", "Number of active connections", ["server"]
)
server_status_gauge = Gauge(
    "server_status", "Status of the Minecraft server (1=running, 0=stopped)", ["server"]
)


class MetricsManager:
    """
    Handles the application's Prometheus metrics, including an async update loop.
    """

    def __init__(self, app_config: AppConfig, docker_manager: DockerManager):
        self.app_config = app_config
        self.docker_manager = docker_manager
        self._is_enabled = app_config.is_prometheus_enabled

        if self._is_enabled:
            # Initialize gauges for all configured servers
            for server in self.app_config.game_servers:
                active_connections_gauge.labels(server=server.name).set(0)
                server_status_gauge.labels(server=server.name).set(0)
            log.info("MetricsManager initialized for Prometheus.")
        else:
            log.info("Prometheus is disabled. MetricsManager will not run.")

    async def start(self):
        """
        Starts the Prometheus HTTP server and the periodic update task
        if Prometheus is enabled.
        """
        if not self._is_enabled:
            return  # Do nothing if Prometheus is not enabled

        try:
            start_http_server(self.app_config.prometheus_port)
            log.info(
                "Prometheus HTTP server started",
                port=self.app_config.prometheus_port,
            )
            # Start the periodic task to update server status
            await self._update_server_status_periodically()
        except asyncio.CancelledError:
            log.info("Metrics manager task cancelled.")
        except Exception:
            log.error("Failed to start Prometheus server", exc_info=True)

    def inc_active_connections(self, server_name: str):
        """Increments the active connections gauge for a server."""
        if self._is_enabled:
            active_connections_gauge.labels(server=server_name).inc()

    def dec_active_connections(self, server_name: str):
        """Decrements the active connections gauge for a server."""
        if self._is_enabled:
            active_connections_gauge.labels(server=server_name).dec()

    async def _update_server_status_periodically(self):
        """
        Periodically checks and updates the running status of each server container.
        """
        log.info("Starting periodic server status updater for metrics.")
        while True:
            for server in self.app_config.game_servers:
                try:
                    is_running = await self.docker_manager.is_container_running(
                        server.container_name
                    )
                    server_status_gauge.labels(server=server.name).set(
                        1 if is_running else 0
                    )
                except Exception:
                    log.error(
                        "Error updating server status metric",
                        server=server.name,
                        exc_info=True,
                    )
            await asyncio.sleep(self.app_config.server_check_interval)
