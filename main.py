import asyncio
import signal
import sys
import threading
import time
from pathlib import Path

import structlog
from prometheus_client import start_http_server

from config import load_application_config
from proxy import NetherBridgeProxy

# Configure logging
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
logger = structlog.get_logger(__name__)

HEARTBEAT_FILE = Path("/tmp/nether_bridge_heartbeat")


def configure_logging(log_level_str: str, formatter: str):
    """Dynamically configures logging settings."""
    log_level = log_level_str.lower()
    if formatter == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level=log_level),
    )


def perform_health_check():
    """Performs a health check and exits."""
    if not HEARTBEAT_FILE.exists():
        logger.error("Health check failed: Heartbeat file not found.")
        sys.exit(1)

    try:
        last_heartbeat = HEARTBEAT_FILE.read_text()
        if time.time() - float(last_heartbeat) > 60:
            logger.error("Health check failed: Heartbeat is stale.")
            sys.exit(1)
    except (ValueError, FileNotFoundError):
        logger.error("Health check failed: Could not read heartbeat file.")
        sys.exit(1)

    logger.info("Health check passed.")
    sys.exit(0)


async def run_proxy_instance_async(proxy: NetherBridgeProxy):
    """
    Runs a single lifecycle of the proxy. Returns True if a reload is requested.
    """
    logger.info("--- Starting Nether-bridge Proxy Instance ---")

    if HEARTBEAT_FILE.exists():
        try:
            HEARTBEAT_FILE.unlink()
        except OSError as e:
            logger.warning("Could not remove stale heartbeat file.", error=str(e))

    proxy.docker_manager.connect()
    proxy._ensure_all_servers_stopped_on_startup()

    monitor_thread = threading.Thread(
        target=proxy._monitor_servers_activity, daemon=True
    )
    monitor_thread.start()

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

    try:
        initial_settings, _ = load_application_config()
        if initial_settings.prometheus_enabled:
            start_http_server(initial_settings.prometheus_port)
            logger.info(
                "Prometheus metrics server started.",
                port=initial_settings.prometheus_port,
            )
    except Exception as e:
        logger.error("Could not start Prometheus server on initial load.", error=str(e))

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

        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, proxy.signal_handler)
        signal.signal(signal.SIGINT, proxy.signal_handler)
        signal.signal(signal.SIGTERM, proxy.signal_handler)

        try:
            reload_requested = asyncio.run(run_proxy_instance_async(proxy))
        except KeyboardInterrupt:
            reload_requested = False

    logger.info("Application has exited gracefully.")


if __name__ == "__main__":
    main()
