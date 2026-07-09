"""Shared CLI runtime: the ExtendedTyper app, console, and per-command helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Coroutine

from rich.console import Console
from typer_extensions import ExtendedTyper

from brewery.core.logging import BreweryLogger, configure_logging, get_logger
from brewery.core.repo import Repository

log: BreweryLogger = get_logger(name=__name__)

app = ExtendedTyper(help="Brewery: A package management CLI tool")

console = Console(emoji=False, highlight=False)


@app.callback()
def setup() -> None:
    """Set up the CLI environment"""
    configure_logging(level="INFO", enable_console=True)


@contextmanager
def _repository() -> Iterator[Repository]:
    """Yield a repository instance and close it on exit."""
    repo = Repository()
    try:
        yield repo

    finally:
        repo.close()


def run_async(coro: Coroutine) -> Any:
    """Run a coroutine to completion.

    Args:
        coro: The coroutine to run.

    Returns:
        The result of the coroutine.
    """
    import asyncio

    return asyncio.run(coro)
