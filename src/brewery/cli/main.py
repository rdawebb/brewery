"""CLI entry point for Brewery package management tool."""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

from rich.console import Console
from typer_extensions import ExtendedTyper

from brewery.cli.renderers import package_details, package_table
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
from brewery.core.logging import configure_logging, get_logger
from brewery.core.models import PackageKind
from brewery.core.repo import Repository

log = get_logger(__name__)

app = ExtendedTyper(help="Brewery: A package management CLI tool")

console = Console()


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
    configure_logging(level="INFO", enable_console=True)


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
    try:
        repo = Repository()
        pkgs = asyncio.run(repo.get_all_installed(kind_filter=kind))

        if outdated:
            pkgs = [p for p in pkgs if "OUTDATED" in str(p.status)]
        if search:
            q = search.lower()
            pkgs = [
                p
                for p in pkgs
                if q in p.name.lower() or (p.desc and q in p.desc.lower())
            ]

        console.print(package_table(pkgs))

    except Exception as e:
        sys.exit(handle_error(e))


@app.command_with_aliases(aliases=["i"])
def info(
    name: str,
    kind: Optional[PackageKind] = app.Option(
        None, "--kind", help="formula | cask | auto (default)"
    ),
) -> None:
    """Show detailed information about a package.

    Args:
        name: Name of the package.
        kind: Kind of the package (formula or cask). If not provided, will auto-detect.
    """
    try:
        repo = Repository()

        if kind is None:
            try:
                all_pkgs = asyncio.run(repo.get_all_installed())
                matching_pkg = next((p for p in all_pkgs if p.name == name), None)
                if matching_pkg:
                    pkg = asyncio.run(repo.get_details(name, matching_pkg.kind))
                else:
                    raise PackageNotFoundError(package=name)

            except PackageNotFoundError:
                raise
        else:
            pkg = asyncio.run(repo.get_details(name, kind))

        console.print(package_details(pkg))

    except Exception as e:
        sys.exit(handle_error(e))


@app.command_with_aliases(aliases=["s", "find"])
def search(term: str) -> None:
    """Search for packages by name or description.

    Args:
        term: Search term.
    """
    try:
        repo = Repository()
        pkgs = asyncio.run(repo.get_all_installed())

        q = term.lower()
        pkgs = [
            p for p in pkgs if q in p.name.lower() or (p.desc and q in p.desc.lower())
        ]

        console.print(package_table(pkgs))

    except Exception as e:
        sys.exit(handle_error(e))


@app.command_with_aliases(aliases=["add"])
def install(
    name: str,
    kind: Optional[PackageKind] = app.Option(
        None, "--kind", help="formula | cask | auto (default)"
    ),
    yes: bool = app.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Install a package.

    Args:
        name: Name of the package to install.
        kind: Kind of the package (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    try:
        kind = kind or PackageKind.FORMULA
        kind_label = kind.value

        if not yes:
            confirmed = app.confirm(f"Install {kind_label} {name}?", default=True)
            if not confirmed:
                console.print("Installation cancelled.", style="dim")
                sys.exit(0)

        repo = Repository()
        with console.status(f"[bold green]Installing {name}...", spinner="dots"):
            pkg = asyncio.run(repo.install_package(name, kind))

        console.print(
            f"\n✅ Installed [bold]{pkg.name}[/bold] "
            f"{pkg.versions[0] if pkg.versions else ''}"
        )

    except Exception as e:
        sys.exit(handle_error(e))


@app.command_with_aliases(aliases=["rm", "remove"])
def uninstall(
    name: str,
    kind: Optional[PackageKind] = app.Option(
        None, "--kind", help="formula | cask | auto (default)"
    ),
    yes: bool = app.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Uninstall a package.

    Args:
        name: Name of the package to uninstall.
        kind: Kind of the package (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    try:
        repo = Repository()

        if kind is None:
            all_pkgs = asyncio.run(repo.get_all_installed())
            matching_pkg = next((p for p in all_pkgs if p.name == name), None)
            if matching_pkg:
                kind = matching_pkg.kind
            else:
                raise PackageNotFoundError(package=name)

        else:
            kind = kind

        kind_label = kind.value

        if not yes:
            confirmed = app.confirm(f"Uninstall {kind_label} {name}?", default=False)
            if not confirmed:
                console.print("Uninstallation cancelled.", style="dim")
                sys.exit(0)

        with console.status(f"[bold red]Uninstalling {name}...", spinner="dots"):
            asyncio.run(repo.uninstall_package(name, kind))

        console.print(f"\n✅ Uninstalled [bold]{name}[/bold]")

    except Exception as e:
        sys.exit(handle_error(e))


if __name__ == "__main__":
    app()
