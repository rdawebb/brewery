"""Centralised logging setup for the Brewery application."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

_CONFIGURED = False

_STDLIB_SPECIAL_KWARGS = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})


class BreweryLogger:
    """Thin wrapper around a stdlib Logger that accepts structlog-style keyword args."""

    def __init__(self, logger: logging.Logger) -> None:
        """Initialise the BreweryLogger with a standard logging.Logger instance.

        Args:
            logger: The logger instance to be wrapped.
        """
        self._logger: logging.Logger = logger

    def _log(self, level: int, event: str, **kwargs: Any) -> None:
        """Log a message at the specified logging level with optional context.

        Args:
            level: The logging level (e.g., logging.DEBUG, logging.INFO).
            event: The main log message to be recorded.
            **kwargs: Additional keyword arguments for context and special logging parameters.
        """
        stdlib_kwargs: dict[str, Any] = {}
        context: dict[str, Any] = {}

        for k, v in kwargs.items():
            if k in _STDLIB_SPECIAL_KWARGS:
                stdlib_kwargs[k] = v
            elif v is not None:
                context[k] = v

        if context:
            suffix: str = " | " + " ".join(f"{k}={v}" for k, v in context.items())
        else:
            suffix = ""

        self._logger.log(level, "%s%s", event, suffix, **stdlib_kwargs)

    def debug(self, event: str = "", **kwargs: Any) -> None:
        """Log a debug message with optional context.

        Args:
            event: The message to log - defaults to an empty string.
            **kwargs: Additional contextual information.
        """
        self._log(level=logging.DEBUG, event=event, **kwargs)

    def info(self, event: str = "", **kwargs: Any) -> None:
        """Log an info message with optional context.

        Args:
            event: The message to log - defaults to an empty string.
            **kwargs: Additional contextual information.
        """
        self._log(level=logging.INFO, event=event, **kwargs)

    def warning(self, event: str = "", **kwargs: Any) -> None:
        """Log a warning message with optional context.

        Args:
            event: The message to log - defaults to an empty string.
            **kwargs: Additional contextual information.
        """
        self._log(level=logging.WARNING, event=event, **kwargs)

    def error(self, event: str = "", **kwargs: Any) -> None:
        """Log an error message with optional context.

        Args:
            event: The message to log - defaults to an empty string.
            **kwargs: Additional contextual information.
        """
        self._log(level=logging.ERROR, event=event, **kwargs)

    def critical(self, event: str = "", **kwargs: Any) -> None:
        """Log a critical message with optional context.

        Args:
            event: The message to log - defaults to an empty string.
            **kwargs: Additional contextual information.
        """
        self._log(level=logging.CRITICAL, event=event, **kwargs)


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
        log_dir: Path = Path(
            os.environ.get("BREWERY_LOG_DIR", Path.home() / ".brewery" / "logs")
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file: Path = log_dir / "backend.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        filename=log_file, maxBytes=1 * 1024 * 1024, backupCount=4
    )
    file_handler.setFormatter(fmt=formatter)
    file_handler.setLevel(level=getattr(logging, level.upper()))
    logging.root.addHandler(hdlr=file_handler)

    if enable_console:
        console_handler: logging.StreamHandler[TextIO] = logging.StreamHandler()
        console_handler.setFormatter(fmt=formatter)
        console_handler.setLevel(level=logging.ERROR)
        logging.root.addHandler(hdlr=console_handler)

    logging.root.setLevel(level=getattr(logging, level.upper()))
    _CONFIGURED = True


def get_logger(name: str = "brewery") -> BreweryLogger:
    """Get a logger instance.

    Args:
        name: Optional name for the logger, typically the module name.

    Returns:
        A BreweryLogger instance.
    """
    return BreweryLogger(logger=logging.getLogger(name))
