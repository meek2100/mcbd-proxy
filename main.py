# main.py
"""
The main entrypoint for the Nether-bridge application.
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


async def amain():
    """The main asynchronous entrypoint for the application."""
    app_config = load_app_config()
    configure_logging(app_config.log_level, app_config.log_format)

    docker_manager = DockerManager(app_config)
    proxy_server = AsyncProxy(app_config, docker_manager)

    try:
        await proxy_server.start()
    except asyncio.CancelledError:
        log.info("Main application task was cancelled.")
    finally:
        log.info("Closing Docker manager session.")
        await docker_manager.close()
        log.info("Shutdown complete.")


def health_check():
    """
    Performs a simple health check. Exits 0 on success, 1 on failure.
    """
    try:
        load_app_config()
        print("Health check passed: Configuration loaded successfully.")
        sys.exit(0)
    except Exception as e:
        print(f"Health check failed: {e}")
        sys.exit(1)


def main():
    """Main entrypoint function to be called by the script."""
    if "--healthcheck" in sys.argv:
        health_check()
    else:
        try:
            asyncio.run(amain())
        except KeyboardInterrupt:
            log.info("Application interrupted by user.")


if __name__ == "__main__":
    main()
