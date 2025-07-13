import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import structlog

from config import load_application_config
from docker_manager import DockerManager
from metrics import (
    ACTIVE_SESSIONS,
    BYTES_TRANSFERRED,
    RUNNING_SERVERS,
    SERVER_STARTUP_DURATION,
    start_metrics_server,
)
from proxy import NetherBridgeProxy

CONFIG_PATH = Path("/app/config")


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


def perform_health_check(config_path: Path):
    """Performs a self-sufficient two-stage health check."""
    logger = structlog.get_logger(__name__)
    heartbeat_file = config_path / "heartbeat.txt"

    try:
        settings, servers_list = load_application_config()
        if not servers_list:
            logger.error("Health Check FAIL: No servers configured.")
            sys.exit(1)
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(
            "Health Check FAIL: Error loading configuration.",
            error=str(e),
            exc_info=True,
        )
        sys.exit(1)

    if not heartbeat_file.is_file():
        logger.error("Health Check FAIL: Heartbeat file not found.")
        sys.exit(1)
    try:
        age = int(time.time()) - int(heartbeat_file.read_text())
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


async def main():
    """The main entrypoint for the Nether-bridge application."""
    early_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    early_log_formatter = os.environ.get("NB_LOG_FORMATTER", "console")
    configure_logging(early_log_level, early_log_formatter)

    if "--healthcheck" in sys.argv:
        perform_health_check(CONFIG_PATH)
        return

    logger = structlog.get_logger(__name__)
    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for signame in ("SIGINT", "SIGTERM"):
            loop.add_signal_handler(
                getattr(signal, signame),
                lambda: asyncio.create_task(shutdown_event.set()),
            )
        if hasattr(signal, "SIGHUP"):
            loop.add_signal_handler(
                signal.SIGHUP,
                lambda: asyncio.create_task(reload_event.set()),
            )
    else:
        logger.warning("Signal handlers are not supported on Windows.")

    try:
        settings, servers = load_application_config()
        configure_logging(settings.log_level, settings.log_formatter)
    except Exception as e:
        logger.critical("FATAL: Error during config loading.", error=str(e))
        sys.exit(1)

    if not servers:
        logger.critical("FATAL: No servers configured. Exiting.")
        sys.exit(1)

    docker_manager = DockerManager(docker_url=settings.docker_url)
    proxy = NetherBridgeProxy(
        settings=settings,
        servers_list=servers,
        docker_manager=docker_manager,
        shutdown_event=shutdown_event,
        reload_event=reload_event,
        active_sessions_metric=ACTIVE_SESSIONS,
        running_servers_metric=RUNNING_SERVERS,
        bytes_transferred_metric=BYTES_TRANSFERRED,
        server_startup_duration_metric=SERVER_STARTUP_DURATION,
        config_path=CONFIG_PATH,
    )

    # Restore the pre-warm startup check
    await proxy.ensure_all_servers_stopped_on_startup()

    if settings.prometheus_enabled:
        try:
            start_metrics_server(settings.prometheus_port)
            logger.info("Prometheus metrics started.", port=settings.prometheus_port)
        except Exception as e:
            logger.error("Could not start Prometheus server.", error=str(e))

    proxy_task = asyncio.create_task(proxy.run())

    try:
        await proxy_task
    except asyncio.CancelledError:
        logger.info("Main proxy task was cancelled.")
    finally:
        logger.info("Initiating graceful shutdown.")
        await proxy._shutdown_all_sessions()
        await proxy._close_listeners()
        await docker_manager.close()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        structlog.get_logger(__name__).info("Application interrupted by user.")
    except Exception as e:
        structlog.get_logger(__name__).critical(
            "Unhandled exception in main app.", error=str(e), exc_info=True
        )
        sys.exit(1)
