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
    # The log_format is now passed in as an argument.
    log_format_lower = log_format.lower()

    # Shared processors for both JSON and console logging
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.threadlocal.merge_threadlocal,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.dict_sort,
    ]

    # Processors specific to the standard library logging framework
    stdlib_processors: list[Processor] = [
        structlog.stdlib.filter_by_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    if log_format_lower == "json":
        # JSON logs for production/CI
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Console logs for local development
        processors = shared_processors + [
            structlog.processors.format_exc_info,
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
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=stdlib_processors,
        )
    )

    root_logger.addHandler(handler)
    root_logger.setLevel(log_level.upper())

    logger = structlog.get_logger("logging_config")
    logger.info(
        "Logging configured", log_format=log_format_lower, log_level=log_level.upper()
    )
