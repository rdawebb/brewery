"""Centralised logging setup for the Brewery application."""

from __future__ import annotations

import logging
import os
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

_CONFIGURED = False


def _default_log_dir() -> Path:
    """The platform-conventional log directory (resolved per call).

    Returns:
        The default log directory for the host.
    """
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Logs" / "brewery"

    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"

    return base / "brewery" / "logs"


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


def ensure_log_dir() -> Path:
    """Resolve and create the log directory shared by the CLI and the daemon.

    Returns:
        $BREWERY_LOG_DIR if set, else the platform-conventional log directory.
    """
    override = os.environ.get("BREWERY_LOG_DIR")
    log_dir = Path(override) if override else _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    return log_dir


def configure_logging(
    level: str = "INFO", log_file: Path | None = None, console_level: str | None = None
) -> None:
    """Configure logging for the Brewery application.

    User-facing errors are printed by the CLI itself, so the console stays free of
    log output unless a console level is requested.

    Args:
        level: The file logging level as a string (e.g., "DEBUG", "INFO").
        log_file: Optional path to a log file for file logging.
        console_level: Level for a stderr handler, e.g. "DEBUG". None disables it.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    if log_file is None:
        log_file = ensure_log_dir() / "backend.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    file_level: int = getattr(logging, level.upper())

    file_handler = RotatingFileHandler(
        filename=log_file, maxBytes=1 * 1024 * 1024, backupCount=4
    )
    file_handler.setFormatter(fmt=formatter)
    file_handler.setLevel(level=file_level)
    logging.root.addHandler(hdlr=file_handler)

    levels: list[int] = [file_level]

    if console_level is not None:
        console_handler: logging.StreamHandler[TextIO] = logging.StreamHandler(
            stream=sys.stderr
        )
        console_handler.setFormatter(fmt=formatter)
        console_handler.setLevel(level=getattr(logging, console_level.upper()))
        logging.root.addHandler(hdlr=console_handler)
        levels.append(console_handler.level)

    logging.root.setLevel(level=min(levels))
    _CONFIGURED = True


def get_logger(name: str = "brewery") -> BreweryLogger:
    """Get a logger instance.

    Args:
        name: Optional name for the logger, typically the module name.

    Returns:
        A BreweryLogger instance.
    """
    return BreweryLogger(logger=logging.getLogger(name))
