import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import structlog
from prometheus_client import start_http_server

# Import the refactored modules
from config import load_application_config
from proxy import NetherBridgeProxy

# The heartbeat file constant remains at the application level
HEARTBEAT_FILE = Path("proxy_heartbeat.tmp")


def configure_logging(log_level: str, log_formatter: str):
    """Configures logging for the application using structlog."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level.upper(),
    )

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
    ]

    if log_formatter == "json":
        processors = shared_processors + [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:  # "console" or any other value
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def perform_health_check():
    """Performs a self-sufficient two-stage health check."""
    logger = structlog.get_logger(__name__)
    try:
        settings, servers_list = load_application_config()
        if not servers_list:
            logger.error("Health Check FAIL: No server configuration found.")
            sys.exit(1)
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(
            "Health Check FAIL: Error loading configuration.",
            error=str(e),
            exc_info=True,
        )
        sys.exit(1)

    if not HEARTBEAT_FILE.is_file():
        logger.error("Health Check FAIL: Heartbeat file not found.")
        sys.exit(1)
    try:
        age = int(time.time()) - int(HEARTBEAT_FILE.read_text())
        if age < settings.healthcheck_stale_threshold_seconds:
            logger.info("Health Check OK", age_seconds=age)
            sys.exit(0)
        else:
            logger.error("Health Check FAIL: Heartbeat is stale.", age_seconds=age)
            sys.exit(1)
    except Exception as e:
        logger.error(
            "Health Check FAIL: Could not read or parse heartbeat file.",
            error=str(e),
            exc_info=True,
        )
        sys.exit(1)


def run_app(proxy: NetherBridgeProxy):
    """
    Starts all services and runs the main proxy loop.
    This was previously the `run` method on the proxy class.
    """
    logger = structlog.get_logger(__name__)
    logger.info("--- Starting Nether-bridge On-Demand Proxy ---")

    if proxy.settings.prometheus_enabled:
        try:
            logger.info(
                "Starting Prometheus metrics server...",
                port=proxy.settings.prometheus_port,
            )
            start_http_server(proxy.settings.prometheus_port)
            logger.info("Prometheus metrics server started.")
        except Exception as e:
            logger.error("Could not start Prometheus metrics server.", error=str(e))
    else:
        logger.info("Prometheus metrics server is disabled by configuration.")

    app_metadata = os.environ.get("APP_IMAGE_METADATA")
    if app_metadata:
        try:
            meta = json.loads(app_metadata)
            logger.info("Application build metadata", **meta)
        except json.JSONDecodeError:
            logger.warning("Could not parse APP_IMAGE_METADATA", metadata=app_metadata)

    if HEARTBEAT_FILE.exists():
        try:
            HEARTBEAT_FILE.unlink()
            logger.info("Removed stale heartbeat file.", path=str(HEARTBEAT_FILE))
        except OSError as e:
            logger.warning("Could not remove stale heartbeat file.", error=str(e))

    # Connect the Docker manager
    proxy.docker_manager.connect()

    # Run startup tasks
    proxy._ensure_all_servers_stopped_on_startup()

    for srv_cfg in proxy.servers_list:
        try:
            proxy._create_listening_socket(srv_cfg)
        except OSError:
            # If a socket fails to bind on startup, it's a fatal error.
            sys.exit(1)

    monitor_thread = threading.Thread(
        target=proxy._monitor_servers_activity, daemon=True
    )
    monitor_thread.start()

    # Pass the main module to the loop for access to configure_logging on reload
    proxy._run_proxy_loop(sys.modules[__name__])


def main():
    """The main entrypoint for the Nether-bridge application."""
    # Early configuration for healthcheck and initial logs.
    early_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    early_log_formatter = os.environ.get("NB_LOG_FORMATTER", "console")
    configure_logging(early_log_level, early_log_formatter)

    if "--healthcheck" in sys.argv:
        perform_health_check()
        return

    try:
        settings, servers = load_application_config()
    except Exception as e:
        # Catch critical config loading errors
        logger = structlog.get_logger(__name__)
        logger.critical(
            "FATAL: A critical error occurred during configuration loading.",
            error=str(e),
        )
        sys.exit(1)

    # Reconfigure with final settings from files/env
    configure_logging(settings.log_level, settings.log_formatter)
    logger = structlog.get_logger(__name__)
    logger.info(
        "Log level and formatter set to final values.",
        log_level=settings.log_level,
        log_formatter=settings.log_formatter,
    )

    if not servers:
        # load_application_config logs the critical error, so we just exit.
        sys.exit(1)

    proxy = NetherBridgeProxy(settings, servers)

    # Set the signal handlers to point to the method on the proxy instance
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, proxy.signal_handler)
    signal.signal(signal.SIGINT, proxy.signal_handler)
    signal.signal(signal.SIGTERM, proxy.signal_handler)

    run_app(proxy)


if __name__ == "__main__":
    main()
