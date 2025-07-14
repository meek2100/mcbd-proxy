# main.py
import asyncio
import signal

import structlog

from config import AppConfig, setup_logging
from docker_manager import DockerManager
from metrics import MetricsManager
from proxy import Proxy


async def main():
    """
    Initializes and runs the proxy application, handling startup and
    graceful shutdown.
    """
    # Load configuration and set up logging
    config = AppConfig()
    setup_logging(config.log_level, config.log_formatter)
    logger = structlog.get_logger(__name__)

    logger.info("Application starting...")

    # Initialize managers
    docker_manager = DockerManager(docker_url=config.docker_url)
    metrics_manager = MetricsManager(
        metrics_port=config.metrics_port,
        metrics_host=config.metrics_host,
    )

    # Start the Prometheus metrics server
    metrics_manager.start_server()
    logger.info(
        "Metrics server started.",
        host=config.metrics_host,
        port=config.metrics_port,
    )

    # Initialize the proxy
    proxy_server = Proxy(config, docker_manager, metrics_manager)

    # --- Graceful Shutdown Setup ---
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    loop.add_signal_handler(signal.SIGTERM, stop.set_result, None)
    loop.add_signal_handler(signal.SIGINT, stop.set_result, None)

    # --- Run the application ---
    try:
        # Start the proxy and wait for a shutdown signal
        await asyncio.gather(proxy_server.start(), stop)
    except Exception as e:
        logger.error("Unhandled exception in main.", error=str(e), exc_info=True)
    finally:
        logger.info("Application shutting down...")
        await proxy_server.stop()
        await docker_manager.close()
        logger.info("Cleanup complete. Exiting.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # The loop handles SIGINT, but this is a fallback
        pass
