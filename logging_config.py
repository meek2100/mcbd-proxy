import logging
import sys

import structlog
from structlog.typing import Processor


def setup_logging(log_level: str = "INFO", log_format: str = "json"):
    """
    Configures logging for the application using structlog.

    The format is determined by the `log_format` parameter.
    - "json": Structured JSON logging for production/CI.
    - "console": Human-readable, colored logs for local development.

    The log level is passed in from the application's configuration.
    """
    log_format_lower = log_format.lower()

    # Shared processors for both JSON and console logging
    shared_processors: list[Processor] = [
        # Add context from structlog.contextvars.bind_contextvars
        structlog.contextvars.merge_contextvars,
        # Remove deprecated structlog.threadlocal
        # structlog.threadlocal.merge_threadlocal,
        # Add basic info about the log record
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    # Processors specific to the standard library logging framework
    stdlib_processors: list[Processor] = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    # Determine the final renderer processor based on the chosen format
    if log_format_lower == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    # Configure the main structlog processor chain
    structlog.configure(
        processors=shared_processors
        + [
            structlog.processors.format_exc_info,
            renderer,  # Use the renderer determined above
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the standard library logging to use structlog processors
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    # Explicitly pass the final renderer to the ProcessorFormatter
    # This is the key fix for the TypeError
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=stdlib_processors,
        )
    )

    root_logger.addHandler(handler)
    root_logger.setLevel(log_level.upper())

    logger = structlog.get_logger("logging_config")
    logger.info(
        "Logging configured", log_format=log_format_lower, log_level=log_level.upper()
    )
