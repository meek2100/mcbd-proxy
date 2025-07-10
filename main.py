import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import structlog
from prometheus_client import start_http_server

from config import load_application_config
from proxy import NetherBridgeProxy

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
        settings, _ = load_application_config()
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


def run_proxy_instance(proxy: NetherBridgeProxy):
    """
    Runs a single lifecycle of the proxy. Returns True if a reload is requested.
    """
    logger = structlog.get_logger(__name__)
    logger.info("--- Starting Nether-bridge Proxy Instance ---")

    if HEARTBEAT_FILE.exists():
        try:
            HEARTBEAT_FILE.unlink()
        except OSError as e:
            logger.warning("Could not remove stale heartbeat file.", error=str(e))

    proxy.docker_manager.connect()
    proxy._ensure_all_servers_stopped_on_startup()

    for srv_cfg in proxy.servers_list:
        try:
            proxy._create_listening_socket(srv_cfg)
        except OSError:
            logger.critical("Fatal error creating listening socket. Exiting.")
            return False  # Exit the main loop

    monitor_thread = threading.Thread(
        target=proxy._monitor_servers_activity, daemon=True
    )
    monitor_thread.start()

    reload_needed = proxy._run_proxy_loop()

    logger.info("Instance shutting down. Cleaning up resources.")
    proxy._shutdown_all_sessions()
    monitor_thread.join(timeout=5.0)
    if monitor_thread.is_alive():
        logger.warning("Monitor thread did not terminate in time.")

    return reload_needed


def main():
    """The main entrypoint for the Nether-bridge application."""
    early_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    early_log_formatter = os.environ.get("NB_LOG_FORMATTER", "console")
    configure_logging(early_log_level, early_log_formatter)
    logger = structlog.get_logger(__name__)

    if "--healthcheck" in sys.argv:
        perform_health_check()
        return

    # Load initial settings to start prometheus if enabled
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

        reload_requested = run_proxy_instance(proxy)

    logger.info("Application has exited gracefully.")


if __name__ == "__main__":
    main()
