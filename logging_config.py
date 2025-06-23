import logging
import os
import sys

import structlog
from structlog.typing import Processor


def setup_logging(log_level: str = "INFO"):
    """
    Configures logging for the application using structlog.

    The format is determined by the `LOG_FORMAT` environment variable.
    - "json" (default): Structured JSON logging for production/CI.
    - "console": Human-readable, colored logs for local development.

    The log level is passed in from the application's configuration.
    """
    log_format = os.environ.get("LOG_FORMAT", "json").lower()

    # Shared processors for both JSON and console logging
    shared_processors: list[Processor] = [
        # Add context from structlog.contextvars.bind_contextvars
        structlog.contextvars.merge_contextvars,
        # Add local thread-specific context
        structlog.threadlocal.merge_threadlocal,
        # Add basic info about the log record
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        # Add a timestamp to the log record.
        structlog.processors.TimeStamper(fmt="iso"),
        # Ensures that "event" is the first key in the log record.
        structlog.processors.dict_sort,
    ]

    # Processors specific to the standard library logging framework
    stdlib_processors: list[Processor] = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    if log_format == "json":
        # JSON logs for production/CI
        processors = shared_processors + [
            # Prepare event dict for JSON rendering.
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Console logs for local development
        processors = shared_processors + [
            # Render exceptions nicely.
            structlog.processors.format_exc_info,
            # Human-readable, colored output.
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    # Configure structlog
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the standard library logging to use structlog processors
    # This ensures that logs from other libraries (like Docker) are also
    # processed by structlog.
    root_logger = logging.getLogger()
    # Mute noisy libraries if necessary
    # logging.getLogger("docker").setLevel(logging.INFO)

    # Clear existing handlers
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    # Use structlog's formatter to process log records from standard logging.
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=stdlib_processors,
        )
    )

    root_logger.addHandler(handler)
    root_logger.setLevel(log_level.upper())

    # Add a message to confirm which logging format is active.
    logger = structlog.get_logger("logging_config")
    logger.info(
        "Logging configured", log_format=log_format, log_level=log_level.upper()
    )
