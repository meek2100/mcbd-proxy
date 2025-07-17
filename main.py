# main.py
"""
The main entrypoint for the Nether-bridge application.
"""

import asyncio
import json
import logging
import os
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


def configure_logging(log_level: str, log_format: str):
    """Configures structured logging for the application."""
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

    if log_format == "json":
        processors = shared_processors + [
            structlog.processors.StackInfoRenderer(),  # Re-introduced
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


async def _update_heartbeat(app_config):
    """
    Periodically writes a timestamp to the heartbeat file to signal liveness.
    Uses configurable interval from app_config.
    """
    while True:
        try:
            current_time = int(time.time())
            HEARTBEAT_FILE.write_text(str(current_time))
            # Use configurable interval for heartbeat
            await asyncio.sleep(app_config.healthcheck_heartbeat_interval)
        except asyncio.CancelledError:
            log.info("Heartbeat task cancelled.")
            break
        except Exception:
            log.error("Failed to update heartbeat file.", exc_info=True)


async def amain():
    """The main asynchronous entrypoint for the application."""
    app_config = load_app_config()

    # Configure logging based on the loaded app_config
    configure_logging(app_config.log_level, app_config.log_format)

    # Add back APP_IMAGE_METADATA logging
    app_metadata = os.environ.get("APP_IMAGE_METADATA")
    if app_metadata:
        try:
            meta = json.loads(app_metadata)
            log.info("Application build metadata", **meta)
        except json.JSONDecodeError:
            log.warning("Could not parse APP_IMAGE_METADATA", metadata=app_metadata)

    # Add check for loaded game servers as per the plan
    if not app_config.game_servers:
        log.critical("FATAL: No server configurations loaded. Exiting.")
        sys.exit(1)

    docker_manager = DockerManager(app_config)
    proxy_server = AsyncProxy(app_config, docker_manager)

    def sighup_handler():
        """Schedules a configuration reload inside the running proxy."""
        log.warning("SIGHUP received, scheduling configuration reload.")
        proxy_server.schedule_reload()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, proxy_server._shutdown_handler, sig)
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, sighup_handler)

    # Pass app_config to heartbeat task
    heartbeat_task = asyncio.create_task(_update_heartbeat(app_config))
    try:
        await proxy_server.start()
    except asyncio.CancelledError:
        log.info("Main application task was cancelled.")
    finally:
        heartbeat_task.cancel()
        log.info("Closing Docker manager session.")
        await docker_manager.close()
        log.info("Shutdown complete.")


def health_check():
    """
    Performs a two-stage health check:
    1. Checks if the configuration can be loaded.
    2. Checks if the heartbeat timestamp in the file is recent.
    """
    try:
        app_config = load_app_config()
        if not HEARTBEAT_FILE.exists():
            print("Health check failed: Heartbeat file not found.")
            sys.exit(1)

        heartbeat_age = int(time.time()) - int(HEARTBEAT_FILE.read_text())
        # Use the configurable threshold from AppConfig
        if heartbeat_age > app_config.healthcheck_stale_threshold:
            print(f"Health check failed: Heartbeat is stale ({heartbeat_age}s).")
            sys.exit(1)

        print(f"Health check passed: Heartbeat is fresh ({heartbeat_age}s).")
        sys.exit(0)
    except (ValueError, FileNotFoundError) as e:
        print(f"Health check failed during execution: {e}")
        sys.exit(1)
    except Exception as e:
        # Catch configuration loading errors for health check
        print(f"Health check failed during config load: {e}")
        sys.exit(1)


def main():
    """Main entrypoint function to be called by the script."""
    # Early configuration to ensure healthcheck can log errors.
    # Uses environment variables directly for initial logging setup.
    early_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    early_log_format = os.environ.get("NB_LOG_FORMATTER", "console")
    configure_logging(early_log_level, early_log_format)

    if "--healthcheck" in sys.argv:
        health_check()
    else:
        try:
            asyncio.run(amain())
        except KeyboardInterrupt:
            log.info("Application interrupted by user.")
        except Exception as e:
            log.critical(
                "Unhandled exception in main application loop.", exc_info=True, error=e
            )
            sys.exit(1)
        finally:
            if HEARTBEAT_FILE.exists():
                HEARTBEAT_FILE.unlink(missing_ok=True)
                log.info("Removed heartbeat file on shutdown.")
