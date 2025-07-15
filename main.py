# main.py
"""
The main entrypoint for the Nether-bridge application.
"""

import asyncio
import signal
import sys
import time
from pathlib import Path

import structlog

from config import load_app_config
from docker_manager import DockerManager
from proxy import AsyncProxy

log = structlog.get_logger()
HEARTBEAT_FILE = Path("proxy_heartbeat.tmp")
RELOAD_CONFIG = False


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


async def _update_heartbeat():
    """Periodically touches the heartbeat file to signal liveness."""
    while True:
        try:
            HEARTBEAT_FILE.touch()
            log.debug("Updated heartbeat file.", path=str(HEARTBEAT_FILE))
            await asyncio.sleep(15)  # Update every 15 seconds
        except asyncio.CancelledError:
            break
        except Exception:
            log.error("Failed to update heartbeat file.", exc_info=True)
            await asyncio.sleep(60)  # Wait longer on error


async def amain():
    """The main asynchronous entrypoint for the application."""
    global RELOAD_CONFIG
    loop = asyncio.get_running_loop()

    def sighup_handler():
        """Sets the flag to trigger a configuration reload."""
        global RELOAD_CONFIG
        log.warning("SIGHUP received, scheduling configuration reload.")
        RELOAD_CONFIG = True

    # SIGHUP is not available on Windows, so we check for its existence
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, sighup_handler)

    docker_manager = None
    while True:
        app_config = load_app_config()
        configure_logging(app_config.log_level, app_config.log_format)

        if not docker_manager:
            docker_manager = DockerManager(app_config)

        # Update the manager's config in case it changed
        docker_manager.app_config = app_config
        proxy_server = AsyncProxy(app_config, docker_manager)

        heartbeat_task = asyncio.create_task(_update_heartbeat())
        proxy_task = asyncio.create_task(proxy_server.start())

        # Wait until a reload is requested
        while not RELOAD_CONFIG:
            await asyncio.sleep(1)

        # --- Reload sequence ---
        log.info("Initiating graceful shutdown for reload...")
        RELOAD_CONFIG = False
        heartbeat_task.cancel()
        proxy_server._shutdown_handler()
        try:
            await asyncio.wait_for(proxy_task, timeout=10.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass  # Expected cancellation
        log.info("Services shut down. Reloading...")


def health_check():
    """
    Performs a two-stage health check:
    1. Checks if the configuration can be loaded.
    2. Checks if the heartbeat file is recent.
    """
    try:
        # Stage 1: Validate configuration loading
        load_app_config()
        log.debug("Health check: Configuration loaded successfully.")

        # Stage 2: Check for a recent heartbeat
        if not HEARTBEAT_FILE.exists():
            print("Health check failed: Heartbeat file not found.")
            sys.exit(1)

        heartbeat_age = time.time() - HEARTBEAT_FILE.stat().st_mtime
        stale_threshold = 60  # seconds

        if heartbeat_age > stale_threshold:
            print(f"Health check failed: Heartbeat is stale ({heartbeat_age:.0f}s).")
            sys.exit(1)

        print(f"Health check passed: Heartbeat is fresh ({heartbeat_age:.0f}s).")
        sys.exit(0)
    except Exception as e:
        print(f"Health check failed during execution: {e}")
        sys.exit(1)


def main():
    """Main entrypoint function to be called by the script."""
    if "--healthcheck" in sys.argv:
        health_check()
    else:
        docker_manager = None
        try:
            asyncio.run(amain())
        except KeyboardInterrupt:
            log.info("Application interrupted by user.")
        except asyncio.CancelledError:
            log.info("Main application task was cancelled.")
        finally:
            if docker_manager:
                asyncio.run(docker_manager.close())
            if HEARTBEAT_FILE.exists():
                HEARTBEAT_FILE.unlink(missing_ok=True)
            log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
