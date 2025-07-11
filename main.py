import asyncio
import signal
import sys
import time

import structlog

from config import ProxySettings, load_application_config
from docker_manager import DockerManager
from proxy import HEARTBEAT_FILE, NetherBridgeProxy

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


async def perform_health_check(settings: ProxySettings):
    if not HEARTBEAT_FILE.is_file():
        logger.error("Health Check FAIL: Heartbeat file not found.")
        sys.exit(1)

    try:
        stale_threshold = getattr(settings, "healthcheck_stale_threshold_seconds", 60)
        age = int(time.time()) - int(HEARTBEAT_FILE.read_text())

        if age < stale_threshold:
            logger.info("Health Check OK", age_seconds=age)
            sys.exit(0)
        else:
            logger.error("Health Check FAIL: Heartbeat is stale.", age_seconds=age)
            sys.exit(1)
    except Exception as e:
        logger.error(
            "Health Check FAIL: Could not read or parse heartbeat file.", error=e
        )
        sys.exit(1)


async def main():
    settings, servers = load_application_config()

    if "--healthcheck" in sys.argv:
        await perform_health_check(settings)
        return

    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()

    def signal_handler(sig):
        if sig == signal.SIGHUP:
            logger.info("SIGHUP received. Triggering configuration reload.")
            reload_event.set()
        else:  # SIGINT, SIGTERM
            logger.info("Shutdown signal received.")
            shutdown_event.set()

    loop = asyncio.get_running_loop()
    # Add SIGHUP handler if the OS supports it
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, signal_handler, signal.SIGHUP)
    loop.add_signal_handler(signal.SIGINT, signal_handler, signal.SIGINT)
    loop.add_signal_handler(signal.SIGTERM, signal_handler, signal.SIGTERM)

    docker_manager = None
    try:
        docker_manager = DockerManager(docker_url=settings.docker_url)
        proxy = NetherBridgeProxy(
            settings, servers, docker_manager, shutdown_event, reload_event
        )
        await proxy.run()
    except Exception as e:
        logger.error("An unhandled exception occurred during main execution", error=e)
    finally:
        if docker_manager:
            await docker_manager.close()
        logger.info("Application has shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user.")
