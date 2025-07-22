# main.py
"""
The main entrypoint for the Nether-bridge application.
"""

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

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


class Application:
    """Manages the lifecycle of the proxy and its background tasks."""

    def __init__(self):
        self.shutdown_event = asyncio.Event()
        self.tasks: List[asyncio.Task] = []
        self.proxy_server: Optional[AsyncProxy] = None

    def _setup_signal_handlers(self):
        """Sets up signal handlers for graceful shutdown and reload."""
        if sys.platform == "win32":
            return  # Signal handlers are not supported on Windows

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._create_shutdown_handler(sig))
        if hasattr(signal, "SIGHUP") and self.proxy_server:
            loop.add_signal_handler(signal.SIGHUP, self.proxy_server.schedule_reload)

    def _create_shutdown_handler(self, sig: signal.Signals):
        """Creates a closure for the shutdown signal handler."""
        return lambda: asyncio.create_task(self.shutdown(sig))

    async def shutdown(self, sig: Optional[signal.Signals] = None):
        """Initiates a graceful shutdown of all application tasks."""
        if self.shutdown_event.is_set():
            return

        log.warning(
            "Shutdown signal received, initiating graceful shutdown...",
            signal=sig.name if sig else "manual",
        )
        self.shutdown_event.set()

        # Allow heartbeat and other loops to exit cleanly
        await asyncio.sleep(0.01)

        if self.proxy_server:
            await self.proxy_server.shutdown()

        for task in self.tasks:
            if not task.done():
                task.cancel()

    async def run(self):
        """Main application entrypoint and lifecycle management."""
        app_config = load_app_config()
        configure_logging(app_config.log_level, app_config.log_format)

        if not app_config.game_servers:
            log.critical("FATAL: No server configurations loaded. Exiting.")
            sys.exit(1)

        docker_manager = DockerManager(app_config)
        self.proxy_server = AsyncProxy(app_config, docker_manager)
        self._setup_signal_handlers()

        try:
            log.info("Nether-bridge is starting...")
            heartbeat_task = asyncio.create_task(
                _update_heartbeat(app_config, self.shutdown_event)
            )
            proxy_task = asyncio.create_task(self.proxy_server.start())
            self.tasks.extend([heartbeat_task, proxy_task])

            # This is the main application loop. It runs until a task fails or
            # shutdown is called. This is more robust than waiting on an event.
            await asyncio.gather(*self.tasks)

        except asyncio.CancelledError:
            log.info("Main application task cancelled.")
        finally:
            log.info("Cleaning up resources...")
            await docker_manager.close()
            log.info("Shutdown complete.")


async def _update_heartbeat(app_config, shutdown_event: asyncio.Event):
    """
    Periodically writes a timestamp to the heartbeat file.
    """
    log.debug("Starting heartbeat update loop.")
    while not shutdown_event.is_set():
        try:
            HEARTBEAT_FILE.write_text(str(int(time.time())))
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=app_config.healthcheck_heartbeat_interval,
            )
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            log.error("Failed to update heartbeat file.", exc_info=True)
            await asyncio.sleep(app_config.healthcheck_heartbeat_interval)


def health_check():
    """Performs a health check by reading the heartbeat file."""
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
    except Exception as e:
        print(f"Health check failed during execution: {e}")
        sys.exit(1)


def main():
    """Synchronous entrypoint to start the application."""
    if "--healthcheck" in sys.argv:
        health_check()
    else:
        app = Application()
        try:
            asyncio.run(app.run())
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Application interrupted by user.")
        except Exception:
            log.critical("Unhandled exception in main application.", exc_info=True)
            sys.exit(1)
        finally:
            if HEARTBEAT_FILE.exists():
                HEARTBEAT_FILE.unlink(missing_ok=True)
            log.debug("Heartbeat file removed on shutdown.")
