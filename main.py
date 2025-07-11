# main.py

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import structlog
from structlog.dev import ConsoleRenderer
from structlog.processors import JSONRenderer
from structlog.stdlib import LoggerFactory, get_logger

from config import load_application_config
from docker_manager import DockerManager
from metrics import (
    ACTIVE_SESSIONS,
    BYTES_TRANSFERRED,
    RUNNING_SERVERS,
    SERVER_STARTUP_DURATION,
    start_metrics_server,  # Import the function
)
from proxy import NetherBridgeProxy

# Setup structlog for consistent logging
structlog.configure(
    processors=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        ConsoleRenderer()
        if os.getenv("NB_LOG_FORMATTER") == "console"
        else JSONRenderer(),
    ],
    logger_factory=LoggerFactory(),
    cache_logger_on_first_use=True,
)
log = get_logger()


def perform_health_check(config_path: Path) -> None:
    """
    Performs a health check by checking the last modified time of a heartbeat
    file.
    """
    heartbeat_file = config_path / "heartbeat.txt"
    if not heartbeat_file.exists():
        log.error(
            "Health check failed: Heartbeat file not found.", file_path=heartbeat_file
        )
        sys.exit(1)

    try:
        last_modified_time = os.path.getmtime(heartbeat_file)
        current_time = time.time()
        # If the heartbeat file hasn't been updated in 60 seconds,
        # consider it unhealthy (this threshold should ideally come from config)
        # For simplicity, using a hardcoded value for the healthcheck here.
        # The proxy itself will use `healthcheck_stale_threshold_seconds` from config.
        if (current_time - last_modified_time) > 60:
            log.error(
                "Health check failed: Heartbeat is stale.",
                last_modified=last_modified_time,
                current_time=current_time,
            )
            sys.exit(1)
        else:
            log.info("Health check successful: Heartbeat is fresh.")
            sys.exit(0)
    except Exception as e:
        log.error("Health check failed: An error occurred.", error=str(e))
        sys.exit(1)


async def main():
    """
    Main asynchronous function to start and manage the NetherBridge proxy.
    """
    parser = argparse.ArgumentParser(
        description="NetherBridge - Minecraft On-Demand Proxy"
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Run health check and exit.",
    )
    args = parser.parse_args()

    # Determine base directory for configuration files
    if getattr(sys, "frozen", False):
        # Running in a PyInstaller bundle
        base_dir = Path(sys.executable).parent
    else:
        # Running in a development environment
        base_dir = Path(__file__).parent

    config_path = base_dir / "config"
    # Ensure config directory exists for heartbeat file
    config_path.mkdir(exist_ok=True)

    if args.healthcheck:
        perform_health_check(config_path)  # This will exit
        return

    # Load application config
    proxy_settings, server_configs = load_application_config()

    log.info(
        "NetherBridge Proxy starting...",
        settings=proxy_settings,
        servers=[s.name for s in server_configs],
    )

    # Log application build metadata if available
    app_metadata = os.environ.get("APP_IMAGE_METADATA")
    if app_metadata:
        try:
            meta = json.loads(app_metadata)
            log.info("Application build metadata", **meta)
        except json.JSONDecodeError:
            log.warning("Could not parse APP_IMAGE_METADATA", metadata=app_metadata)

    docker_manager = None
    proxy = None
    shutdown_event = asyncio.Event()
    reload_event = asyncio.Event()

    try:
        docker_manager = DockerManager(proxy_settings.docker_url)
        proxy = NetherBridgeProxy(
            proxy_settings,
            server_configs,
            docker_manager,
            shutdown_event,
            reload_event,
            ACTIVE_SESSIONS,  # Pass metric objects
            RUNNING_SERVERS,
            BYTES_TRANSFERRED,
            SERVER_STARTUP_DURATION,
            config_path,  # Pass the config_path for heartbeat file management
        )

        # Start Prometheus metrics server if enabled
        if proxy_settings.prometheus_enabled:
            start_metrics_server(proxy_settings.prometheus_port)
        else:
            log.info("Prometheus metrics server is disabled by configuration.")

        # Signal handlers for graceful shutdown and reload
        loop = asyncio.get_running_loop()

        def handle_signal():
            log.info("Shutdown signal received.")
            shutdown_event.set()

        def handle_reload_signal():
            log.info("Reload signal received.")
            reload_event.set()

        if os.name != "nt":  # Windows does not support SIGTERM/SIGHUP directly
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, handle_signal)
            loop.add_signal_handler(signal.SIGHUP, handle_reload_signal)
        else:
            log.warning(
                "Signal handlers for SIGTERM/SIGHUP are not available on Windows."
            )

        # Main proxy task
        proxy_task = asyncio.create_task(proxy.run())

        # Periodically check for reload requests from signal and update heartbeat
        while not shutdown_event.is_set():
            if reload_event.is_set():
                log.info("Reloading configuration (main loop detected)...")
                # Reload configuration and pass to proxy
                reloaded_proxy_settings, reloaded_server_configs = (
                    load_application_config()
                )
                await proxy._reload_configuration(
                    reloaded_proxy_settings,
                    reloaded_server_configs,
                )
                reload_event.clear()
                log.info("Configuration reloaded.")

            # Short sleep to prevent busy-waiting if no signals
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass  # Continue loop if no shutdown event

        await proxy_task  # Wait for the proxy to finish its shutdown routine

    except asyncio.CancelledError:
        log.info("Application tasks cancelled.")
    except Exception as e:
        log.exception("An unhandled error occurred in main.", error=str(e))
        sys.exit(1)
    finally:
        if proxy:
            log.info("Shutting down proxy gracefully (from main).")
            # Ensure proxy listeners are closed and sessions terminated
            await proxy._close_listeners()
            await proxy._shutdown_all_sessions()
        if docker_manager:
            log.info("Closing Docker manager client.")
            await docker_manager.close()
        log.info("NetherBridge Proxy stopped.")
        # Clean up heartbeat file if it was created by the main process
        heartbeat_file_path = config_path / "heartbeat.txt"
        if heartbeat_file_path.exists():
            heartbeat_file_path.unlink(missing_ok=True)
            log.info("Cleaned up heartbeat file.", path=heartbeat_file_path)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        structlog.get_logger().info("Application interrupted by user (Ctrl+C).")
    except Exception:
        structlog.get_logger().exception("Application terminated due to error.")
        sys.exit(1)
