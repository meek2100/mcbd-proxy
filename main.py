import logging
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
        format="%(message)s", stream=sys.stdout, level=log_level.upper()
    )
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
    ]
    processors = shared_processors + (
        [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
        if log_formatter == "json"
        else [structlog.dev.ConsoleRenderer(colors=True)]
    )
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
        logger.debug("Health Check: Configuration loaded OK.")
    except Exception as e:
        logger.error("Health Check FAIL: Config loading error.", error=str(e))
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
            "Health Check FAIL: Heartbeat file error.", error=str(e), exc_info=True
        )
        sys.exit(1)


def run_app(proxy: NetherBridgeProxy):
    """Starts all services and runs the main proxy loop."""
    logger = structlog.get_logger(__name__)
    logger.info("--- Starting Nether-bridge On-Demand Proxy ---")

    if proxy.settings.prometheus_enabled:
        start_http_server(proxy.settings.prometheus_port)
        logger.info("Prometheus metrics server started.")

    if HEARTBEAT_FILE.exists():
        HEARTBEAT_FILE.unlink()

    proxy.docker_manager.connect()

    # Run initial cleanup in the background to not block the main loop
    threading.Thread(
        target=proxy._ensure_all_servers_stopped_on_startup, daemon=True
    ).start()

    for srv_cfg in proxy.servers_list:
        proxy._create_listening_socket(srv_cfg)

    monitor_thread = threading.Thread(
        target=proxy._monitor_servers_activity, daemon=True
    )
    monitor_thread.start()

    proxy._run_proxy_loop(sys.modules[__name__])

    logger.info("Graceful shutdown initiated.")
    proxy._shutdown_all_sessions()
    monitor_thread.join(timeout=5.0)
    logger.info("Shutdown complete. Exiting.")


def main():
    """The main entrypoint for the Nether-bridge application."""
    if "--healthcheck" in sys.argv:
        # Health checks should be quiet unless they fail
        configure_logging("INFO", "json")
        perform_health_check()
        return

    settings, servers = load_application_config()
    configure_logging(settings.log_level, settings.log_formatter)

    if not servers:
        sys.exit(1)

    proxy = NetherBridgeProxy(settings, servers)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, proxy.signal_handler)
    signal.signal(signal.SIGINT, proxy.signal_handler)
    signal.signal(signal.SIGTERM, proxy.signal_handler)

    run_app(proxy)


if __name__ == "__main__":
    main()
