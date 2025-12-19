"""Centralised logging setup for the Brewery application."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog
from structlog.types import FilteringBoundLogger

_CONFIGURED = False


def sanitise_context(logger: Any, method_name: str, event_dict: dict) -> dict:
    """Sanitize context by removing None values and ensuring error is a string.

    This processor ensures that structlog processors don't crash when encountering
    None values in the context dictionary.

    Args:
        logger: The logger instance.
        method_name: The name of the method called on the logger.
        event_dict: The event dictionary to sanitize.

    Returns:
        The sanitized event dictionary.
    """
    sanitised = {k: v for k, v in event_dict.items() if v is not None}

    if "error" in sanitised and sanitised["error"] is None:
        sanitised["error"] = ""

    return sanitised


def configure_logging(
    level: str = "INFO", log_file: Path | None = None, enable_console: bool = False
) -> None:
    """Configure logging for the Brewery application.

    Args:
        level: The logging level as a string (e.g., "DEBUG", "INFO").
        log_file: Optional path to a log file for file logging.
        enable_console: Whether to enable console logging.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    if log_file is None:
        log_dir = Path.home() / ".brewery" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "backend.log"

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=2)
    file_handler.setLevel(getattr(logging, level.upper()))

    shared_processors = [
        sanitise_context,
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if enable_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(getattr(logging, level.upper()))
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.root.addHandler(console_handler)

        structlog.configure(
            processors=shared_processors
            + [
                structlog.processors.ExceptionRenderer(),
                structlog.dev.ConsoleRenderer(colors=True),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=shared_processors
            + [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()],
            wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

    logging.root.setLevel(getattr(logging, level.upper()))
    logging.root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str = "brewery") -> FilteringBoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Optional name for the logger, typically the module name.

    Returns:
        A structlog FilteringBoundLogger instance.

    Usage:
        log = get_logger(__name__)
        log.info("event_name", package="foo", duration_ms=123)

    Standard context keys:
        - event (str): Name of operation or event
        - package (str): Name of the package or module
        - kind (str): "formula" or "cask"
        - duration_ms (int): Operation duration in milliseconds
        - error (str): Error message if applicable
        - exc_info (bool): Whether exception info is included
    """
    if not _CONFIGURED:
        configure_logging()
    return structlog.get_logger(name)
