"""CLI entry point for Brewery package management tool."""

from __future__ import annotations

import time

script_start_time = time.perf_counter()

import asyncio
import sys
from typing import Optional

print(f"Builtin imports: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")

from typer_extensions import ExtendedTyper

print(
    f"typer-extensions import: {(time.perf_counter() - script_start_time) * 1000:.2f} ms"
)
from rich.console import Console

print(f"rich import: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")

from brewery.cli.renderers import package_details, package_table

print(f"renderers import: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")
from brewery.core.errors import (
    EXIT_SYSTEM_ERROR,
    EXIT_TRANSIENT_ERROR,
    EXIT_USER_ERROR,
    BrewError,
    PackageNotFoundError,
    SystemError,
    TransientError,
    UserError,
    format_error_message,
    suggest_search,
)

print(f"errors import: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")
from brewery.core.logging import configure_logging, get_logger

print(f"logging import: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")
from brewery.core.models import PackageKind

print(f"models import: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")
from brewery.core.repo import Repository

print(f"repo import: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")

log = get_logger(__name__)
print(f"Logger setup: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")

app = ExtendedTyper(help="Brewery: A package management CLI tool")
print(f"Typer app setup: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")

console = Console()
print(f"Rich console setup: {(time.perf_counter() - script_start_time) * 1000:.2f} ms")


def handle_error(error: Exception) -> int:
    """Handle errors and return appropriate exit codes.

    Args:
        error: The exception to handle.

    Returns:
        An integer exit code.
    """
    if isinstance(error, BrewError):
        try:
            log.error(
                "cli_error",
                error_type=type(error).__name__,
                message=error.message,
                context=getattr(error, "context", {}),
                exc_info=True,
            )
        except Exception:
            pass
        console.print(f"\n{format_error_message(error)}\n", style="bold red")

        if isinstance(error, PackageNotFoundError):
            package = getattr(error, "context", {}).get("package", "")
            console.print(suggest_search(package), style="dim")

        if isinstance(error, TransientError):
            return EXIT_TRANSIENT_ERROR
        elif isinstance(error, UserError):
            return EXIT_USER_ERROR
        elif isinstance(error, SystemError):
            return EXIT_SYSTEM_ERROR
        else:
            return EXIT_USER_ERROR
    else:
        log.error("unexpected_error", error=str(error), exc_info=True)
        console.print(f"\n⚠️ Unexpected error occurred: {error}\n", style="bold red")
        return EXIT_SYSTEM_ERROR


@app.callback()
def setup() -> None:
    """Set up the CLI environment"""
    now = time.time()
    configure_logging(level="INFO", enable_console=True)
    print(f"After logging setup: {(time.time() - now) * 1000:.2f} ms")


@app.command_with_aliases(aliases=["ls", "l"])
def list(
    kind: Optional[PackageKind] = app.Option(
        None, "--kind", "-k", help="formula | cask | all"
    ),
    outdated: bool = app.Option(False, help="Only outdated"),
    search: Optional[str] = app.Option(None, "--search", "-s", help="Filter by text"),
) -> None:
    """List packages in the repository.

    Args:
        kind: Filter by package kind.
        outdated: If true, only show outdated packages.
        search: Text to filter package names/descriptions.
    """
    start_time = time.perf_counter()
    print(f"Time since script start: {(start_time - script_start_time) * 1000:.2f} ms")
    try:
        print(f"Before repository: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        repo = Repository()
        print(f"After repository: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        pkgs = asyncio.run(repo.get_all_installed(kind_filter=kind))
        print(
            f"After get_all_installed: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )
        if outdated:
            pkgs = [p for p in pkgs if "OUTDATED" in str(p.status)]
        if search:
            q = search.lower()
            pkgs = [
                p
                for p in pkgs
                if q in p.name.lower() or (p.desc and q in p.desc.lower())
            ]
        print(f"After filtering: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        console.print(package_table(pkgs))
        print(
            f"Total function time: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )
        print(
            f"Script total time: {(time.perf_counter() - script_start_time) * 1000:.2f} ms"
        )
    except Exception as e:
        sys.exit(handle_error(e))


@app.command_with_aliases(aliases=["in", "i"])
def info(
    name: str, kind: PackageKind = app.Option(PackageKind.FORMULA, "--kind")
) -> None:
    """Show detailed information about a package.

    Args:
        name: Name of the package.
        kind: Kind of the package (formula or cask).
    """
    start_time = time.perf_counter()
    try:
        print(f"Before repository: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        repo = Repository()
        print(f"After repository: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        pkg = asyncio.run(repo.get_details(name, kind))
        print(f"After get_details: {(time.perf_counter() - start_time) * 1000:.2f} ms")

        console.print(package_details(pkg))
        print(
            f"Total function time: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )
        print(
            f"Script total time: {(time.perf_counter() - script_start_time) * 1000:.2f} ms"
        )
    except Exception as e:
        sys.exit(handle_error(e))


@app.command_with_aliases(aliases=["s", "find"])
def search(term: str) -> None:
    """Search for packages by name or description.

    Args:
        term: Search term.
    """
    start_time = time.perf_counter()
    try:
        print(f"Before repository: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        repo = Repository()
        print(f"After repository: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        pkgs = asyncio.run(repo.get_all_installed())
        print(
            f"After get_all_installed: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )
        q = term.lower()
        pkgs = [
            p for p in pkgs if q in p.name.lower() or (p.desc and q in p.desc.lower())
        ]

        print(f"After filtering: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        console.print(package_table(pkgs))
        print(
            f"Total function time: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )
        print(
            f"Script total time: {(time.perf_counter() - script_start_time) * 1000:.2f} ms"
        )
    except Exception as e:
        sys.exit(handle_error(e))


if __name__ == "__main__":
    app()
