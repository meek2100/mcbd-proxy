"""
Centralized Prometheus metrics definitions for the Nether-bridge application.

By defining these in a separate module, we ensure they are created as singletons
only once when this module is first imported by any part of the application,
preventing "Duplicated timeseries" errors during testing.
"""

from prometheus_client import Counter, Gauge, Histogram

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
