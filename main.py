# main.py

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import structlog
from prometheus_client import start_http_server

# Import the refactored modules
from config import load_application_config
from proxy import NetherBridgeProxy

# The heartbeat file constant remains at the application level
HEARTBEAT_FILE = Path("proxy_heartbeat.tmp")


def configure_logging(log_level: str, log_formatter: str):
    """Configures logging for the application using structlog."""
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

    if log_formatter == "json":
        processors = shared_processors + [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:  # "console" or any other value
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def perform_health_check():
    """Performs a self-sufficient two-stage health check."""
    logger = structlog.get_logger(__name__)
    try:
        # We still need to load settings to get the threshold.
        # It's okay if the healthcheck process itself doesn't find servers.
        settings, _ = load_application_config()
        logger.debug("Health Check Stage 1 (Configuration) OK.")
    except Exception as e:
        logger.error(
            "Health Check FAIL: Error loading configuration.",
            error=str(e),
            exc_info=True,
        )
        sys.exit(1)

    if not HEARTBEAT_FILE.is_file():
        logger.error("Health Check FAIL: Heartbeat file not found.")
        sys.exit(1)
    try:
        age = int(time.time()) - int(HEARTBEAT_FILE.read_text())
        if age < settings.healthcheck_stale_threshold_seconds:
            logger.info("Health Check OK", age_seconds=age)
            sys.exit(0)
        else:
            logger.error("Health Check FAIL: Heartbeat is stale.", age_seconds=age)
            sys.exit(1)
    except Exception as e:
        logger.error(
            "Health Check FAIL: Could not read or parse heartbeat file.",
            error=str(e),
            exc_info=True,
        )
        sys.exit(1)


async def run_app(proxy: NetherBridgeProxy):
    """
    Starts all services and runs the main proxy loop asynchronously.
    """
    logger = structlog.get_logger(__name__)
    logger.info("--- Starting Nether-bridge On-Demand Proxy ---")

    # Set up asyncio signal handlers
    # This must be done inside an async function where an event loop is running.
    loop = asyncio.get_running_loop()
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(
            signal.SIGHUP,
            lambda: asyncio.create_task(proxy.handle_reload_signal()),
        )
    loop.add_signal_handler(
        signal.SIGINT,
        lambda: asyncio.create_task(proxy.handle_shutdown_signal()),
    )
    loop.add_signal_handler(
        signal.SIGTERM,
        lambda: asyncio.create_task(proxy.handle_shutdown_signal()),
    )
    logger.info("Signal handlers registered for graceful shutdown/reload.")

    if proxy.settings.prometheus_enabled:
        try:
            logger.info(
                "Starting Prometheus metrics server...",
                port=proxy.settings.prometheus_port,
            )
            start_http_server(proxy.settings.prometheus_port)
            logger.info("Prometheus metrics server started.")
        except Exception as e:
            logger.error("Could not start Prometheus metrics server.", error=str(e))
    else:
        logger.info("Prometheus metrics server is disabled by configuration.")

    app_metadata = os.environ.get("APP_IMAGE_METADATA")
    if app_metadata:
        try:
            meta = json.loads(app_metadata)
            logger.info("Application build metadata", **meta)
        except json.JSONDecodeError:
            logger.warning("Could not parse APP_IMAGE_METADATA", metadata=app_metadata)

    if HEARTBEAT_FILE.exists():
        try:
            # unlink is typically synchronous and fast, but for robustness
            # in a purely async context, it could be run in a thread pool.
            # For this simple case, direct call is fine.
            HEARTBEAT_FILE.unlink()
            logger.info("Removed stale heartbeat file.", path=str(HEARTBEAT_FILE))
        except OSError as e:
            logger.warning("Could not remove stale heartbeat file.", error=str(e))

    # DockerManager is already initialized in NetherBridgeProxy's __init__
    # So, no need for the problematic proxy.docker_manager_init call.

    # Run startup tasks
    await proxy._ensure_all_servers_stopped_on_startup()

    # Create and start listener tasks
    listener_tasks = []
    for srv_cfg in proxy.servers_list:
        try:
            # _create_listening_socket returns the asyncio.Server or
            # DatagramTransport instance, which needs to be added to tasks.
            # proxy._create_listening_socket now returns a coroutine.
            listener = await proxy._create_listening_socket(srv_cfg)
            # For TCP, start_serving needs to be awaited.
            # For UDP, the transport is already created.
            if hasattr(listener, "start_serving"):
                await listener.start_serving()
            listener_tasks.append(listener)
        except OSError as e:
            logger.critical(
                "FATAL: Could not bind to port. Exiting.",
                port=srv_cfg.listen_port,
                error=str(e),
                exc_info=True,
            )
            # Ensure cleanup before exiting, if possible
            for task in listener_tasks:
                if not task.done():
                    task.cancel()
            # If listeners are still running as tasks, gather them to finish
            # their cleanup.
            await asyncio.gather(*listener_tasks, return_exceptions=True)
            await proxy.docker_manager.close()  # Ensure docker client is closed
            sys.exit(1)

    # Start the background monitoring task
    monitor_task = asyncio.create_task(proxy._monitor_activity())

    logger.info("Nether-bridge proxy is fully operational.")

    try:
        # Keep the application running indefinitely, waiting for tasks
        # to complete or cancellation.
        # This will block until the event loop is stopped, e.g., by a signal.
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("Application cancelled, initiating graceful shutdown.")
    finally:
        # --- SHUTDOWN SEQUENCE STARTS HERE ---
        logger.info("Graceful shutdown initiated.")

        # 1. Cancel the monitor task
        if not monitor_task.done():
            monitor_task.cancel()
            # Await the task to ensure it processes cancellation
            await asyncio.shield(monitor_task)
        logger.info("Monitor task terminated.")

        # 2. Close all active player sessions immediately
        await proxy._shutdown_all_sessions()
        logger.info("All active sessions closed.")

        # 3. Stop all listening sockets/transports
        # For TCP, call close on the server. For UDP, close the transport.
        for listener in listener_tasks:
            if hasattr(listener, "close"):  # For TCP asyncio.Server
                listener.close()
                await listener.wait_closed()
            elif hasattr(listener, "abort"):  # For UDP DatagramTransport
                listener.abort()
        # No need to gather tasks as they are already managed by the listeners.
        logger.info("All listening sockets/transports closed.")

        # 4. Close Docker client
        await proxy.docker_manager.close()
        logger.info("Docker client closed.")

        logger.info("Shutdown complete. Exiting.")


def main():
    """The main entrypoint for the Nether-bridge application."""
    # Early configuration for healthcheck and initial logs.
    early_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    early_log_formatter = os.environ.get("NB_LOG_FORMATTER", "console")
    configure_logging(early_log_level, early_log_formatter)

    if "--healthcheck" in sys.argv:
        perform_health_check()
        return

    try:
        settings, servers = load_application_config()
    except Exception as e:
        # Catch critical config loading errors
        logger = structlog.get_logger(__name__)
        logger.critical(
            "FATAL: A critical error occurred during configuration loading.",
            error=str(e),
        )
        sys.exit(1)

    # Reconfigure with final settings from files/env
    configure_logging(settings.log_level, settings.log_formatter)
    logger = structlog.get_logger(__name__)
    logger.info(
        "Log level and formatter set to final values.",
        log_level=settings.log_level,
        log_formatter=settings.log_formatter,
    )

    if not servers:
        # load_application_config logs the critical error, so we just exit.
        sys.exit(1)

    # Initialize proxy (DockerManager is now initialized within proxy)
    proxy = NetherBridgeProxy(settings, servers)

    try:
        # asyncio.run handles loop creation, running the coroutine,
        # and closing the loop. It should be the single entry point.
        asyncio.run(run_app(proxy))
    except KeyboardInterrupt:
        logger.info("Application interrupted by user (Ctrl+C).")
    except SystemExit:
        # Allow SystemExit from inside run_app to pass through for healthcheck
        # or fatal config errors.
        pass
    except Exception as e:
        logger.critical(
            "Unhandled exception in main application.", error=str(e), exc_info=True
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

