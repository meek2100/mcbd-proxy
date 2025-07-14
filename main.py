# main.py
"""
The main entrypoint for the Nether-bridge application.
Initializes and runs the primary asynchronous proxy server.
"""

import asyncio
import sys

import structlog

from config import load_app_config
from docker_manager import DockerManager
from proxy import AsyncProxy

log = structlog.get_logger()


def configure_logging(log_level: str, log_format: str):
    """Configures structured logging for the application."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            (
                structlog.processors.JSONRenderer()
                if log_format == "json"
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    log.info("Logging configured", level=log_level, format=log_format)


async def amain():
    """The main asynchronous entrypoint for the application."""
    # The application config is now loaded only once here.
    try:
        app_config = load_app_config()
        configure_logging(app_config.log_level, app_config.log_format)
    except Exception:
        log.critical("Fatal: Failed to load application configuration.", exc_info=True)
        sys.exit(1)

    docker_manager = DockerManager(app_config)
    proxy_server = AsyncProxy(app_config, docker_manager)

    try:
        await proxy_server.start()
    except asyncio.CancelledError:
        log.info("Main application task was cancelled by shutdown signal.")
    except Exception:
        log.critical("The main proxy server has crashed.", exc_info=True)
    finally:
        log.info("Closing Docker manager session...")
        await docker_manager.close()
        log.info("Shutdown complete.")


if __name__ == "__main__":
    # The health check is now removed from the main application flow
    # and will be handled by a separate, dedicated script or command if needed,
    # simplifying the main entrypoint.

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Application interrupted by user (Ctrl+C). Shutting down.")
