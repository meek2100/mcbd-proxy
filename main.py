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
from typing import Optional

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
            structlog.processors.StackInfoRenderer(),
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


async def _update_heartbeat(app_config, shutdown_event: asyncio.Event):
    """
    Periodically writes a timestamp to the heartbeat file to signal liveness.
    """
    log.debug("Starting heartbeat update loop.")
    while not shutdown_event.is_set():
        try:
            current_time = int(time.time())
            HEARTBEAT_FILE.write_text(str(current_time))
            log.debug("Heartbeat updated.", timestamp=current_time)
            # Shield the wait so it can't be cancelled directly
            await asyncio.wait_for(
                asyncio.shield(shutdown_event.wait()),
                timeout=app_config.healthcheck_heartbeat_interval,
            )
        except asyncio.TimeoutError:
            continue  # This is the normal loop condition
        except asyncio.CancelledError:
            log.debug("Heartbeat task cancelled.")
            break
        except Exception:
            log.error("Failed to update heartbeat file.", exc_info=True)
            # Prevent a fast busy-loop on persistent file errors
            await asyncio.sleep(app_config.healthcheck_heartbeat_interval)


async def shutdown(
    sig: Optional[signal.Signals],
    proxy_server: AsyncProxy,
    shutdown_event: asyncio.Event,
    tasks_to_cancel: list,
):
    """Gracefully shutdown tasks and set the shutdown event."""
    if shutdown_event.is_set():
        return

    log.warning(
        "Shutdown signal received, initiating graceful shutdown...",
        signal=sig.name if sig else "UNKNOWN",
    )
    # First, stop new connections and trigger cleanup loops
    shutdown_event.set()

    # Allow a brief moment for loops to break
    await asyncio.sleep(0.01)

    # Shut down the proxy server itself (closes listeners, sessions)
    if proxy_server:
        await proxy_server.shutdown()

    # Cancel the main background tasks managed by amain
    for task in tasks_to_cancel:
        if not task.done():
            task.cancel()


async def amain():
    """The main asynchronous entrypoint for the application."""
    app_config = load_app_config()
    configure_logging(app_config.log_level, app_config.log_format)

    app_metadata = os.environ.get("APP_IMAGE_METADATA")
    if app_metadata:
        try:
            meta = json.loads(app_metadata)
            log.info("Application build metadata", **meta)
        except json.JSONDecodeError:
            log.warning("Could not parse APP_IMAGE_METADATA", metadata=app_metadata)

    if not app_config.game_servers:
        log.critical("FATAL: No server configurations loaded. Exiting.")
        sys.exit(1)

    shutdown_event = asyncio.Event()
    docker_manager = DockerManager(app_config)
    proxy_server = AsyncProxy(app_config, docker_manager)
    main_tasks = []

    # This handler needs access to the tasks list to cancel them
    def create_shutdown_handler(s):
        return lambda: asyncio.create_task(
            shutdown(s, proxy_server, shutdown_event, main_tasks)
        )

    # Register signal handlers only on non-Windows platforms
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, create_shutdown_handler(sig))
        if hasattr(signal, "SIGHUP"):
            loop.add_signal_handler(signal.SIGHUP, proxy_server.schedule_reload)

    try:
        # Start background tasks
        heartbeat_task = asyncio.create_task(
            _update_heartbeat(app_config, shutdown_event)
        )
        proxy_task = asyncio.create_task(proxy_server.start())
        main_tasks.extend([heartbeat_task, proxy_task])

        log.info("Nether-bridge is running. Waiting for shutdown signal...")
        await shutdown_event.wait()
        log.info("Shutdown event received, cleaning up tasks.")

    finally:
        # Final cleanup gather ensures all tasks are complete before exit
        await asyncio.gather(*main_tasks, return_exceptions=True)
        log.debug("Closing Docker manager session.")
        await docker_manager.close()
        log.info("Shutdown complete.")


def health_check():
    """
    Performs a two-stage health check.
    """
    try:
        app_config = load_app_config()
        if not HEARTBEAT_FILE.exists():
            print("Health check failed: Heartbeat file not found.")
            sys.exit(1)

        heartbeat_age = int(time.time()) - int(HEARTBEAT_FILE.read_text())
        if heartbeat_age > app_config.healthcheck_stale_threshold:
            print(f"Health check failed: Heartbeat is stale ({heartbeat_age}s).")
            sys.exit(1)

        print(f"Health check passed: Heartbeat is fresh ({heartbeat_age}s).")
        sys.exit(0)
    except (ValueError, FileNotFoundError) as e:
        print(f"Health check failed during execution: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Health check failed during config load: {e}")
        sys.exit(1)


def main():
    """Main entrypoint function to be called by the script."""
    early_log_level = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    early_log_format = os.environ.get("NB_LOG_FORMATTER", "console")
    configure_logging(early_log_level, early_log_format)

    if "--healthcheck" in sys.argv:
        health_check()
    else:
        try:
            asyncio.run(amain())
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Application interrupted by user.")
        except Exception:
            # This will catch any unexpected error during amain() startup
            log.critical("Unhandled exception in main application loop.", exc_info=True)
            sys.exit(1)
        finally:
            if HEARTBEAT_FILE.exists():
                HEARTBEAT_FILE.unlink(missing_ok=True)
                log.debug("Removed heartbeat file on shutdown.")
