# metrics.py
"""
Manages Prometheus metrics for the proxy.
"""

import asyncio

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from config import AppConfig
from docker_manager import DockerManager

log = structlog.get_logger()

# --- Define Prometheus Metrics ---

# Gauge for the current number of active connections per server.
active_connections_gauge = Gauge(
    "netherbridge_active_connections",
    "Number of active connections",
    ["server"],
)

# Gauge for the running status of a server (1=running, 0=stopped).
server_status_gauge = Gauge(
    "netherbridge_server_status",
    "Status of the Minecraft server (1=running, 0=stopped)",
    ["server"],
)

# New: Gauge for the total count of running servers (aggregate)
total_running_servers_gauge = Gauge(
    "netherbridge_total_running_servers",
    "Total count of Minecraft server containers currently running",
)

# Histogram to track the time it takes for servers to start.
server_startup_duration_histogram = Histogram(
    "netherbridge_server_startup_duration_seconds",
    "Time taken for a server to start and become queryable",
    ["server"],
)

# Counter for the total bytes transferred through the proxy.
bytes_transferred_counter = Counter(
    "netherbridge_bytes_transferred_total",
    "Total bytes transferred through the proxy",
    ["server", "direction"],
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
            # Initialize aggregate gauge
            total_running_servers_gauge.set(0)
            log.info("MetricsManager initialized for Prometheus.")
        else:
            log.info("Prometheus is disabled. MetricsManager will not run.")

    async def start(self):
        """
        Starts the Prometheus HTTP server and the periodic update task
        if Prometheus is enabled.
        """
        if not self._is_enabled:
            return

        try:
            start_http_server(self.app_config.prometheus_port)
            log.info(
                "Prometheus HTTP server started",
                port=self.app_config.prometheus_port,
            )
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

    def observe_startup_duration(self, server_name: str, duration: float):
        """Observes the server startup duration."""
        if self._is_enabled:
            server_startup_duration_histogram.labels(server=server_name).observe(
                duration
            )

    def inc_bytes_transferred(self, server_name: str, direction: str, size: int):
        """Increments the bytes transferred counter."""
        if self._is_enabled:
            bytes_transferred_counter.labels(
                server=server_name, direction=direction
            ).inc(size)

    async def _update_server_status_periodically(self):
        """
        Periodically checks and updates the running status of each server
        container and the total count.
        """
        log.info("Starting periodic server status updater for metrics.")
        while True:
            running_count = 0
            for server in self.app_config.game_servers:
                try:
                    is_running = await self.docker_manager.is_container_running(
                        server.container_name
                    )
                    server_status_gauge.labels(server=server.name).set(
                        1 if is_running else 0
                    )
                    if is_running:
                        running_count += 1
                except Exception:
                    log.error(
                        "Error updating server status metric",
                        server=server.name,
                        exc_info=True,
                    )
            # Update the aggregate running servers count
            total_running_servers_gauge.set(running_count)
            # CORRECTED: Use the correct attribute from the AppConfig model
            await asyncio.sleep(self.app_config.player_check_interval)
