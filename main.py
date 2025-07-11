import asyncio
import signal
import sys
import threading
import time

import structlog

from config import load_application_config
from metrics import start_metrics_server
from proxy import HEARTBEAT_INTERVAL_SECONDS, NetherBridgeProxy

# Initialize logger
structlog.configure(
    processors=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger(__name__)


def _heartbeat_writer():
    """Writes a heartbeat message to the log periodically."""
    while True:
        logger.debug("Heartbeat: Nether-bridge is alive.")
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def run_proxy_instance_async(proxy: NetherBridgeProxy):
    """Runs a single proxy instance asynchronously."""
    # Ensure all managed servers are stopped on startup
    await proxy._ensure_all_servers_stopped_on_startup()

    # Start background threads for monitoring and heartbeat
    monitor_thread = threading.Thread(
        target=proxy._monitor_servers_activity, daemon=True
    )
    monitor_thread.start()

    heartbeat_thread = threading.Thread(target=_heartbeat_writer, daemon=True)
    heartbeat_thread.start()

    # Start the Prometheus metrics server in a non-blocking way
    metrics_thread = threading.Thread(
        target=start_metrics_server, args=(proxy.settings.prometheus_port,), daemon=True
    )
    metrics_thread.start()

    reload_requested = await proxy._run_proxy_loop()
    return reload_requested


def main():
    """Main function to load configuration and run the proxy."""
    while True:
        logger.info("Loading configuration...")
        try:
            settings, servers = load_application_config()
            proxy = NetherBridgeProxy(settings, servers)
            logger.info("Configuration loaded successfully. Starting proxy...")
        except Exception as e:
            logger.critical(f"Failed to load configuration or initialize proxy: {e}")
            sys.exit(1)

        # Register signal handlers for graceful shutdown and reload
        signal.signal(signal.SIGINT, proxy.signal_handler)
        signal.signal(signal.SIGTERM, proxy.signal_handler)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, proxy.signal_handler)

        reload_requested = asyncio.run(run_proxy_instance_async(proxy))

        if not reload_requested:
            logger.info("Shutdown complete. Exiting.")
            break
        else:
            logger.info("Reloading configuration and restarting proxy...")


if __name__ == "__main__":
    main()
