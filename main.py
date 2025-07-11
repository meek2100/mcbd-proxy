# main.py
import asyncio
import signal
import sys

import structlog

from config import load_application_config
from docker_manager import DockerManager
from proxy import NetherBridgeProxy

# Configure structured logging for the application
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(min_level="INFO"),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


async def main():
    """Initializes and runs the Nether-bridge proxy server."""
    shutdown_event = asyncio.Event()

    def _handle_shutdown_signal(sig):
        """Sets the shutdown event when a signal is received."""
        logger.warning("Shutdown signal received", signal=sig.name)
        shutdown_event.set()

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_shutdown_signal, sig)

    try:
        settings, servers = load_application_config()
        if not servers:
            logger.critical("No servers configured. Exiting.")
            sys.exit(1)

        docker_manager = DockerManager()
        await docker_manager.connect()

        proxy = NetherBridgeProxy(settings, servers, docker_manager, shutdown_event)
        await proxy.run()

    except Exception:
        logger.exception("An unhandled exception occurred during main execution")
    finally:
        logger.info("Application has shut down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, exiting.")
