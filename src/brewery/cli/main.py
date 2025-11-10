"""CLI entry point for Brewery package management tool."""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

import typer

from brewery.cli.renderers import console, package_details, package_table
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
from brewery.core.logging import configure_logging
from brewery.core.models import PackageKind
from brewery.core.repo import Repository

log = configure_logging(__name__)

app = typer.Typer(help="Brewery: A package management CLI tool.")

configure_logging(level="INFO", enable_console=True)


def handle_error(error: Exception) -> int:
    """Handle errors and return appropriate exit codes.
    
    Args:
        error: The exception to handle.
        
    Returns:
        An integer exit code.
    """
    if isinstance(error, BrewError):
        log.error(
            "cli_error",
            error_type=type(error).__name__,
            message=error.message,
            context=error.context,
            exc_info=True
        )
        console.print(f"\n{format_error_message(error)}\n", style="bold red")

        if isinstance(error, PackageNotFoundError):
            package = error.context.get("package", "")
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
        log.error(
            "unexpected_error",
            error=str(error),
            exc_info=True
        )
        console.print(
            f"\n⚠️ Unexpected error occurred: {error}\n",
            style="bold red"
        )
        return EXIT_SYSTEM_ERROR

@app.command()
def list(
    kind: Optional[PackageKind] = typer.Option(
        None, "--kind", "-k", help="formula | cask | all"
    ),
    outdated: bool = typer.Option(False, help="Only outdated"),
    search: Optional[str] = typer.Option(
        None, "--search", "-s", help="Filter by text"
    )
) -> None:
    """List packages in the repository.
    
    Args:
        kind: Filter by package kind.
        outdated: If true, only show outdated packages.
        search: Text to filter package names/descriptions.
    """
    try:
        repo = Repository()
        pkgs = asyncio.run(repo.get_all_installed(kind_filter=kind))
        if outdated:
            pkgs = [p for p in pkgs if "OUTDATED" in str(p.status)]
        if search:
            q = search.lower()
            pkgs = [p for p in pkgs if q in p.name.lower() or (p.desc and q in p.desc.lower())]

        console.print(package_table(pkgs))
    except Exception as e:
        sys.exit(handle_error(e))

@app.command()
def info(name: str, kind: PackageKind = typer.Option(PackageKind.FORMULA, "--kind")) -> None:
    """Show detailed information about a package.
    
    Args:
        name: Name of the package.
        kind: Kind of the package (formula or cask).
    """
    try:
        repo = Repository()
        pkg = asyncio.run(repo.get_details(name, kind))

        console.print(package_details(pkg))
    except Exception as e:
        sys.exit(handle_error(e))

@app.command()
def search(term: str) -> None:
    """Search for packages by name or description.
    
    Args:
        term: Search term.
    """
    try:
        repo = Repository()
        pkgs = asyncio.run(repo.get_all_installed())
        q = term.lower()
        pkgs = [p for p in pkgs if q in p.name.lower() or (p.desc and q in p.desc.lower())]

        console.print(package_table(pkgs))
    except Exception as e:
        sys.exit(handle_error(e))


if __name__ == "__main__":
    app()