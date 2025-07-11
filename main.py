# main.py

import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path

import structlog
from prometheus_client import start_http_server

from config import load_application_config  # Assuming load_application_config is used
from proxy import NetherBridgeProxy

# Constants for heartbeat file
HEARTBEAT_FILE = Path("/tmp/nether_bridge_heartbeat")
DEFAULT_HEARTBEAT_INTERVAL = (
    5  # Matches NB_HEARTBEAT_INTERVAL in docker-compose.tests.yml
)


def configure_logging(log_level_str: str, formatter: str):
    """Dynamically configures logging settings."""
    log_level = log_level_str.lower()
    if formatter == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    # Reconfigure structlog with the chosen renderer and min_level
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level=log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # Ensure the root logger (used by non-structlog libraries) also respects
    # the desired level if structured logging isn't fully integrated everywhere.
    # This often involves setting the standard library logger's level too.
    # For now, relying on structlog.make_filtering_bound_logger.


logger = structlog.get_logger(__name__)


def perform_health_check():
    """Performs a health check and exits."""
    if not HEARTBEAT_FILE.exists():
        logger.error("Health check failed: Heartbeat file not found.")
        sys.exit(1)
        return

    try:
        last_heartbeat = HEARTBEAT_FILE.read_text()

        # Retrieve stale threshold from env var for health check.
        stale_threshold = int(os.getenv("NB_HEALTHCHECK_STALE_THRESHOLD", "10"))

        if time.time() - float(last_heartbeat) > stale_threshold:
            logger.error("Health check failed: Heartbeat is stale.")
            sys.exit(1)
            return
    except (ValueError, FileNotFoundError):
        logger.error("Health check failed: Could not read heartbeat file.")
        sys.exit(1)
        return

    logger.info("Health check passed.")
    sys.exit(0)


def _heartbeat_writer(
    heartbeat_file: Path, interval: int, shutdown_event: asyncio.Event
):
    """
    Periodically writes a timestamp to a file to signal the application's liveness.
    Runs in a separate thread because it uses blocking time.sleep.
    """
    logger.info("Heartbeat writer started.")
    while not shutdown_event.is_set():
        try:
            heartbeat_file.write_text(str(time.time()))
        except Exception as e:
            logger.error("Failed to write heartbeat file.", error=str(e))
        time.sleep(interval)
    logger.info("Heartbeat writer stopped.")


async def run_proxy_instance_async(proxy: NetherBridgeProxy):
    """
    Runs a single lifecycle of the proxy. Returns True if a reload is requested.
    """
    logger.info("--- Starting Nether-bridge Proxy Instance ---")

    # Ensure stale heartbeat file is removed on startup
    if HEARTBEAT_FILE.exists():
        try:
            HEARTBEAT_FILE.unlink()
        except OSError as e:
            logger.warning("Could not remove stale heartbeat file.", error=str(e))

    proxy.docker_manager.connect()
    # Ensure this is called with appropriate initial boot wait time.
    proxy._ensure_all_servers_stopped_on_startup()

    # Start the monitor thread
    monitor_thread = threading.Thread(
        target=proxy._monitor_servers_activity, daemon=True
    )
    monitor_thread.start()
    logger.info("Server activity monitor thread started.")

    reload_needed = await proxy._run_proxy_loop()

    logger.info("Instance shutting down. Cleaning up resources.")
    monitor_thread.join(timeout=5.0)
    if monitor_thread.is_alive():
        logger.warning("Monitor thread did not terminate in time.")

    return reload_needed


def main():
    """The main entrypoint for the Nether-bridge application."""
    if "--healthcheck" in sys.argv:
        perform_health_check()
        return

    # Initial logging configuration for early startup messages
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level="info"),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    global logger  # Ensure the logger is updated if it's already defined globally
    logger = structlog.get_logger(__name__)

    initial_settings = None  # Initialize to None
    try:
        # Load config to get prometheus and log settings early
        initial_settings, _ = load_application_config()
        if initial_settings.prometheus_enabled:
            start_http_server(initial_settings.prometheus_port)
            logger.info(
                "Prometheus metrics server started.",
                port=initial_settings.prometheus_port,
            )
    except Exception as e:
        logger.error("Could not start Prometheus server on initial load.", error=str(e))
        # Allow the app to continue if Prometheus fails, it's optional.

    reload_requested = True
    is_first_run = True
    while reload_requested:
        if not is_first_run:
            logger.info("Reloading configuration and restarting proxy logic...")
        is_first_run = False

        try:
            settings, servers = load_application_config()
        except Exception as e:
            logger.critical("FATAL: Error loading configuration.", error=str(e))
            sys.exit(1)

        configure_logging(settings.log_level, settings.log_formatter)

        if not servers:
            logger.critical("No servers configured. Exiting.")
            sys.exit(1)

        proxy = NetherBridgeProxy(settings, servers)

        # Start the heartbeat writer immediately when the main loop starts
        heartbeat_interval = getattr(
            settings, "proxy_heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL
        )
        heartbeat_thread = threading.Thread(
            target=_heartbeat_writer,
            args=(HEARTBEAT_FILE, heartbeat_interval, proxy._shutdown_event),
            daemon=True,
        )
        heartbeat_thread.start()
        logger.info("Application heartbeat thread started.")

        # Register signal handlers for the main thread that then set the event
        # These will be called by OS, not directly in asyncio loop here
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, proxy.signal_handler)
        signal.signal(signal.SIGINT, proxy.signal_handler)
        signal.signal(signal.SIGTERM, proxy.signal_handler)

        try:
            reload_requested = asyncio.run(run_proxy_instance_async(proxy))
        except KeyboardInterrupt:
            # Handle Ctrl+C gracefully
            logger.info("KeyboardInterrupt received. Shutting down.")
            reload_requested = False
        finally:
            # Ensure heartbeat thread is signaled to stop on shutdown/reload
            proxy._shutdown_event.set()  # This event is shared
            heartbeat_thread.join(timeout=5.0)
            if heartbeat_thread.is_alive():
                logger.warning("Heartbeat thread did not terminate gracefully.")

    logger.info("Application has exited gracefully.")


if __name__ == "__main__":
    main()
