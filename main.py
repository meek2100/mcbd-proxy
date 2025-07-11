import asyncio
import signal

import structlog

from config import load_application_config
from docker_manager import DockerManager
from proxy import NetherBridgeProxy

# Configure structured logging
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()


async def main():
    """Initializes and runs the proxy application."""
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Shutdown signal received.")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, signal_handler)
    loop.add_signal_handler(signal.SIGTERM, signal_handler)

    docker_manager = None
    try:
        settings, servers = load_application_config()
        docker_manager = DockerManager(docker_url=settings.docker_url)

        # The manager now connects automatically, no .connect() needed.

        proxy = NetherBridgeProxy(settings, servers, docker_manager, shutdown_event)
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
