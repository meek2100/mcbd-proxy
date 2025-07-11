import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Initialize logger for this module
logger = structlog.get_logger(__name__)

# A gauge to track the number of active player sessions.
# Labeled by server name to allow for per-server monitoring.
ACTIVE_SESSIONS = Gauge(
    "netherbridge_active_sessions",
    "Number of active player sessions",
    ["server_name"],
)

# A gauge to track the total number of running Minecraft server containers.
RUNNING_SERVERS = Gauge(
    "netherbridge_running_servers",
    "Number of Minecraft server containers currently running",
)

# A histogram to observe the startup duration of Minecraft servers.
# This helps in identifying performance issues with server startup times.
SERVER_STARTUP_DURATION = Histogram(
    "netherbridge_server_startup_duration_seconds",
    "Time taken for a server to start and become ready",
    ["server_name"],
)

# A counter for the total bytes transferred through the proxy.
# Labeled by server name and direction (client-to-server or server-to-client).
BYTES_TRANSFERRED = Counter(
    "netherbridge_bytes_transferred_total",
    "Total bytes transferred through the proxy",
    ["server_name", "direction"],
)


def start_metrics_server(port: int = 8000):
    """
    Starts the Prometheus metrics HTTP server.
    """
    try:
        start_http_server(port)
        logger.info("Prometheus metrics server started successfully.", port=port)
    except Exception as e:
        logger.error(
            "Failed to start Prometheus metrics server.", port=port, error=str(e)
        )
        # Depending on criticality, you might want to re-raise or handle differently
