"""Shared CLI runtime: the ExtendedTyper app, console, and per-command helpers."""

from __future__ import annotations

import os
import sys
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from rich.console import Console
from typer_extensions import ExtendedTyper

from brewery import __version__
from brewery.core.logging import BreweryLogger, configure_logging, get_logger
from brewery.core.repo import Repository

log: BreweryLogger = get_logger(name=__name__)

app = ExtendedTyper(help="Brewery: A package management CLI tool")

console = Console(emoji=False, highlight=False)


def _print_version(value: bool) -> None:
    """Print the Brewery version and exit, when --version was passed.

    Args:
        value: Whether the --version flag was present.
    """
    if not value:
        return

    console.print(f"\nBrewery [green]{__version__}[/green]\n", style="bold")
    sys.exit(0)


@app.callback()
def setup(
    # Unused by the body: the eager callback prints and exits during parsing
    version: Annotated[
        bool,
        app.Option(
            "--version",
            "-v",
            help="Show the Brewery version and exit",
            callback=_print_version,
            is_eager=True,
            expose_value=False,
        ),
    ] = False,
) -> None:
    """Set up the CLI environment"""
    configure_logging(console_level=os.environ.get("BREWERY_LOG_CONSOLE"))


@contextmanager
def _repository() -> Iterator[Repository]:
    """Yield a repository instance and close it on exit.

    Populates the catalog first if it is empty, which happens on a first run and
    after a schema-version rebuild.

    Yields:
        The repository instance.
    """
    repo = Repository()
    try:
        _ensure_catalog_populated(repo)
        yield repo

    finally:
        repo.close()


def _ensure_catalog_populated(repo: Repository) -> None:
    """Refresh the catalog in the foreground when it holds no formulae.

    Args:
        repo: The repository whose catalog to check and populate.
    """
    if not repo.catalog.is_empty():
        return

    from brewery.cli.output import spinner
    from brewery.daemon.catalog_refresh import refresh_catalog

    try:
        with spinner("Building the package catalog..."):
            run_async(coro=refresh_catalog(catalog=repo.catalog))

    # Bootstrapping is best-effort: the command still runs on an empty catalog
    except Exception as e:  # noqa: BLE001
        log.warning(event="catalog_bootstrap_failed", error=str(object=e))


def run_async(coro: Coroutine) -> Any:
    """Run a coroutine to completion.

    Args:
        coro: The coroutine to run.

    Returns:
        The result of the coroutine.
    """
    import asyncio

    return asyncio.run(coro)
