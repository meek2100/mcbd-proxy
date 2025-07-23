# main.py
"""
The main entrypoint for the Nether-bridge application.

This revised version implements a more robust asyncio lifecycle management
to prevent premature shutdowns.
"""

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

import structlog
from structlog.stdlib import BoundLogger

from config import AppConfig, load_app_config
from docker_manager import DockerManager
from proxy import AsyncProxy

log: BoundLogger = structlog.get_logger()
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

    def __init__(self, app_config: AppConfig):
        self.app_config = app_config
        self.shutdown_event = asyncio.Event()
        self.tasks: List[asyncio.Task] = []
        # These are initialized in start() to ensure they are created
        # within a running asyncio event loop.
        self.docker_manager: Optional[DockerManager] = None
        self.proxy_server: Optional[AsyncProxy] = None

    def _setup_signal_handlers(self):
        """Sets up signal handlers for graceful shutdown."""
        if sys.platform == "win32":
            log.warning("Signal handlers are not supported on Windows.")
            return

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._signal_handler, sig)
        if hasattr(signal, "SIGHUP") and self.proxy_server:
            loop.add_signal_handler(signal.SIGHUP, self.proxy_server.schedule_reload)

    def _signal_handler(self, sig: signal.Signals):
        """Handles shutdown signals by setting the shutdown_event."""
        log.warning(
            "Shutdown signal received, initiating graceful shutdown...",
            signal=sig.name,
        )
        self.shutdown_event.set()

    async def start(self):
        """Initializes async components and starts background tasks."""
        log.info("Nether-bridge is starting...")

        # **FIX:** Instantiate async components here, inside the event loop.
        self.docker_manager = DockerManager(self.app_config)
        self.proxy_server = AsyncProxy(self.app_config, self.docker_manager)

        self._setup_signal_handlers()

        # Start the core application tasks
        proxy_task = asyncio.create_task(self.proxy_server.start())
        heartbeat_task = asyncio.create_task(
            _update_heartbeat(self.app_config, self.shutdown_event)
        )
        self.tasks.extend([proxy_task, heartbeat_task])

        # Monitor tasks for unexpected failures
        monitor_task = asyncio.create_task(self._monitor_tasks())
        self.tasks.append(monitor_task)
        log.info("Application components started successfully.")

    async def _monitor_tasks(self):
        """Monitors running tasks and triggers shutdown if any fail unexpectedly."""
        done, pending = await asyncio.wait(
            self.tasks, return_when=asyncio.FIRST_COMPLETED
        )

        for task in done:
            if task.cancelled():
                continue
            if exc := task.exception():
                log.error(
                    "A critical task failed, initiating shutdown.",
                    task_name=task.get_name(),
                    exc_info=exc,
                )
                self.shutdown_event.set()

    async def run_forever(self):
        """Runs the application until a shutdown is triggered."""
        await self.start()
        await self.shutdown_event.wait()
        await self.cleanup()

    async def cleanup(self):
        """Gracefully stops all tasks and cleans up resources."""
        log.info("Cleaning up resources...")

        for task in self.tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*self.tasks, return_exceptions=True)

        if self.proxy_server:
            await self.proxy_server.shutdown()
        if self.docker_manager:
            await self.docker_manager.close()

        if HEARTBEAT_FILE.exists():
            HEARTBEAT_FILE.unlink(missing_ok=True)
            log.debug("Heartbeat file removed on shutdown.")

        log.info("Shutdown complete.")


async def _update_heartbeat(app_config: AppConfig, shutdown_event: asyncio.Event):
    """Periodically writes a timestamp to the heartbeat file."""
    interval = app_config.healthcheck_heartbeat_interval
    while not shutdown_event.is_set():
        try:
            HEARTBEAT_FILE.write_text(str(int(time.time())))
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            log.error("Failed to update heartbeat file.", exc_info=True)
            await asyncio.sleep(interval)


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
    """Synchronous entrypoint to configure and start the application."""
    if "--healthcheck" in sys.argv:
        health_check()
        return

    # Load config and configure logging synchronously first.
    app_config = load_app_config()
    configure_logging(app_config.log_level, app_config.log_format)

    if not app_config.game_servers:
        log.critical("FATAL: No server configurations loaded. Exiting.")
        sys.exit(1)

    app = Application(app_config)
    try:
        asyncio.run(app.run_forever())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Application interrupted by user.")
    except Exception:
        log.critical("Unhandled exception in main application.", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
