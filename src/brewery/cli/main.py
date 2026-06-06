"""CLI entry point for Brewery package management tool."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Coroutine, Optional

from rich.console import Console
from typer_extensions import ExtendedTyper

from brewery.core.errors import (
    EXIT_SYSTEM_ERROR,
    EXIT_TRANSIENT_ERROR,
    EXIT_USER_ERROR,
    AlreadyInstalledWarning,
    BrewError,
    PackageNotFoundError,
    PinnedPackageWarning,
    SysError,
    TransientError,
    UserError,
    format_error_message,
    suggest_search,
)
from brewery.core.logging import BreweryLogger, configure_logging, get_logger
from brewery.core.models import Package, PackageKind
from brewery.core.repo import Repository
from brewery.daemon.daemon import daemon_app

log: BreweryLogger = get_logger(name=__name__)

app = ExtendedTyper(help="Brewery: A package management CLI tool")
app.add_typer(
    daemon_app,
    name="daemon",
    help="Daemon: Manage the Brewery background refresh daemon.",
)

console = Console(emoji=False, highlight=False)

KNOWN_COMMANDS: set[str] = {
    # List commands/aliases
    "list",
    "ls",
    "l",
    # Info commands/aliases
    "info",
    "i",
    "in",
    # Search commands/aliases
    "search",
    "s",
    "find",
    # Install commands/aliases
    "install",
    "add",
    # Uninstall commands/aliases
    "uninstall",
    "rm",
    "remove",
    # Outdated commands/aliases
    "outdated",
    "o",
    "out",
    # Upgrade commands/aliases
    "upgrade",
    "u",
    "up",
    # Daemon commands/aliases
    "daemon",
}


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
                event="cli_error",
                error_type=type(error).__name__,
                message=error.message,
                context=getattr(error, "context", {}),
                exc_info=True,
            )
        except Exception:
            pass
        console.print(f"\n{format_error_message(error)}\n", style="bold red")

        if isinstance(error, PackageNotFoundError):
            package: str = getattr(error, "context", {}).get("package", "")
            console.print(suggest_search(package_name=package), style="dim")

        if isinstance(error, TransientError):
            return EXIT_TRANSIENT_ERROR
        elif isinstance(error, UserError):
            return EXIT_USER_ERROR
        elif isinstance(error, SysError):
            return EXIT_SYSTEM_ERROR
        else:
            return EXIT_USER_ERROR

    else:
        log.error(event="unexpected_error", error=str(object=error), exc_info=True)
        console.print(f"\n⚠ Unexpected error occurred: {error}\n", style="bold red")
        return EXIT_SYSTEM_ERROR


def _brew_passthrough(argv: list[str]) -> int:
    """Forward an unknown brewery command straight to brew.

    Args:
        argv: The command and arguments to pass to brew.

    Returns:
        The exit code of the brew command.
    """
    if shutil.which("brew") is None:
        console.print("\n❌ brew not found on PATH\n", style="bold red")
        return EXIT_SYSTEM_ERROR

    try:
        return subprocess.run(["brew", *argv]).returncode

    except FileNotFoundError:
        console.print("\n❌ brew not found\n", style="bold red")
        return EXIT_SYSTEM_ERROR

    except KeyboardInterrupt:
        return 130


@contextmanager
def _repository() -> Iterator[Repository]:
    """Yield a repository instance and close it on exit."""
    repo = Repository()
    try:
        yield repo

    finally:
        repo.close()


def _async_run(coro: Coroutine) -> Any:
    """Run a coroutine with the task manager.

    Args:
        coro: The coroutine to run.

    Returns:
        The result of the coroutine.
    """
    return asyncio.run(coro)


@app.callback()
def setup() -> None:
    """Set up the CLI environment"""
    configure_logging(level="INFO", enable_console=True)


@app.command_with_aliases(name="list", aliases=["ls", "l"])
def list_pkgs(
    kind: Optional[PackageKind] = app.Option(
        None, "--kind", "-k", help="formula | cask | all"
    ),
    refresh: bool = app.Option(False, "--refresh", "-r", help="Refresh cache"),
) -> None:
    """List packages in the repository.

    Args:
        kind: Filter by package kind.
        refresh: Refresh cache before listing packages.
    """
    from brewery.cli.renderers import _terminal_size, package_table, paginate

    try:
        with _repository() as repo:
            pkgs: list[Package]

            if refresh:
                with console.status(
                    status="[bold yellow]Refreshing cache...[/bold yellow]",
                    refresh_per_second=6,
                ):
                    repo.cache_mgr.invalidate()
                    pkgs = _async_run(coro=repo.get_all_installed(kind_filter=kind))
            else:
                pkgs = _async_run(coro=repo.get_all_installed(kind_filter=kind))

            _, term_height = _terminal_size()
            page_size: int = term_height - 6  # header + footer buffer

            if len(pkgs) > page_size:
                paginate(pkgs=pkgs, page_size=page_size, console=console)
            else:
                console.print(package_table(pkgs), emoji=False)

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command_with_aliases(aliases=["i", "in"])
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
    from brewery.cli.renderers import package_details

    try:
        with _repository() as repo:
            pkg: Package = _async_run(coro=repo.get_details(name, kind))

            console.print(package_details(pkg))

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command_with_aliases(aliases=["s", "find"])
def search(term: str) -> None:
    """Search for packages by name or description.

    Args:
        term: Search term.
    """
    try:
        with _repository() as repo:
            pkgs: list[Package] = _async_run(coro=repo.search(term))
            from brewery.cli.renderers import package_table

            console.print(package_table(pkgs))

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command_with_aliases(aliases=["add"])
def install(
    names: list[str] = app.Argument(...),
    kind: Optional[PackageKind] = app.Option(
        None, "--kind", help="formula | cask (default: formula)"
    ),
    yes: bool = app.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Install a package or list of packages.

    Args:
        names: Name(s) of the package(s) to install.
        kind: Kind of the package(s) (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    try:
        kind: PackageKind = kind or PackageKind.FORMULA
        if not yes:
            pkg_str: str = ", ".join(names)
            if not app.confirm(text=f"Install {kind.value}: {pkg_str}?", default=True):
                console.print("Installation cancelled.", style="dim")
                return

        with _repository() as repo:
            with console.status(
                status="[bold green]Installing...", refresh_per_second=6
            ):
                installed, failures = _async_run(
                    coro=repo.install_packages(names, kind)
                )

            for pkg in installed:
                console.print(
                    f"[green]✓ Installed [bold]{pkg.name}[/bold] {pkg.versions[0] if pkg.versions else ''}[/green]"
                )
            for name, reason in failures:
                console.print(f"[bold red] Failed {name}: {reason}[/bold red]")

    except AlreadyInstalledWarning as e:
        console.print(f"\n[bold yellow]⚠ {e.message}[/bold yellow]\n")

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command_with_aliases(aliases=["rm", "remove"])
def uninstall(
    names: list[str],
    kind: Optional[PackageKind] = app.Option(
        None, "--kind", help="formula | cask | auto (default)"
    ),
    yes: bool = app.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Uninstall a package or list of packages.

    Args:
        names: Name(s) of the package(s) to uninstall.
        kind: Kind of the package(s) (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    pkg_str: str = ", ".join(names)

    try:
        if not yes:
            if not app.confirm(text=f"Uninstall: {pkg_str}?", default=False):
                console.print("Uninstallation cancelled.", style="dim")
                return

        with _repository() as repo:
            with console.status(
                status=f"[bold yellow]Uninstalling...{pkg_str}", refresh_per_second=6
            ):
                count, failures = _async_run(coro=repo.uninstall_packages(names, kind))

            console.print(f"✓ Uninstalled {count} package(s)")
            for name, reason in failures:
                console.print(f"[bold red]✗ Failed {name}: {reason}[/bold red]")

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command_with_aliases(aliases=["o", "out"])
def outdated(
    check: bool = app.Option(
        False,
        "--check",
        "-c",
        help="Live check for outdated packages and refresh cache",
    ),
) -> None:
    """List outdated packages.

    By default, filters from the local cache — instant but only as fresh
    as the last install/uninstall/check. Pass --check to query brew directly.

    Args:
        check: If True, performs a live brew outdated check and updates cache.
    """
    try:
        with _repository() as repo:
            pkgs: list[Package]

            if check:
                console.print()
                with console.status(
                    status="[bold yellow]Checking for updates...[/bold yellow]",
                    refresh_per_second=6,
                ):
                    pkgs = _async_run(coro=repo.get_outdated(live=True))

            else:
                pkgs = _async_run(coro=repo.get_outdated(live=False))

            if not pkgs:
                console.print(
                    "\n[bold green]✓ All packages are up to date![/bold green]\n"
                )
                return

            from brewery.cli.renderers import package_table

            console.print(package_table(pkgs))
            console.print(
                f"\n[dim] - {len(pkgs)} outdated package(s)"
                f"\n - Run [bold]brewery upgrade[/bold] to update all outdated packages, "
                f"\n   or [bold]brewery upgrade <packages>[/bold] to update specific packages\n"
            )

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command_with_aliases(aliases=["u", "up"])
def upgrade(
    names: Optional[list[str]] = app.Argument(
        None, help="Package(s) to upgrade (leave empty to upgrade all)"
    ),
    kind: Optional[PackageKind] = app.Option(
        None, "--kind", help="formula | cask | auto (default)"
    ),
    yes: bool = app.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Upgrade one, list, or all outdated packages.

    Args:
        names: Name(s) of the package(s) to upgrade (if None, upgrades all outdated).
        kind: Kind of the package (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    try:
        with _repository() as repo:
            if not yes:
                if names:
                    pkg_str: str = ", ".join(names)
                    if not app.confirm(text=f"Upgrade: {pkg_str}?", default=True):
                        console.print("Upgrade cancelled.", style="dim")
                        return

                else:
                    outdated: list[Package] = _async_run(
                        coro=repo.get_outdated(live=False)
                    )
                    if not outdated:
                        console.print(
                            "\n[bold green]✓ All packages are up to date![/bold green]\n"
                        )
                        return

                    from brewery.cli.renderers import package_table

                    console.print(package_table(pkgs=outdated))

                    if not app.confirm(
                        text=f"Upgrade {len(outdated)} outdated package(s)?",
                        default=True,
                    ):
                        console.print("Upgrade cancelled.", style="dim")
                        return

            console.print()
            with console.status(
                status="[bold yellow]Upgrading...[/bold yellow]", refresh_per_second=6
            ):
                upgraded, current, failures = _async_run(
                    coro=repo.upgrade_packages(names, kind)
                )

            if not upgraded and not failures and not current:
                console.print(
                    "\n[bold green]✓ All packages are up to date![/bold green]\n"
                )
                return

            console.print(
                f"[bold green]✓ Upgraded {len(upgraded)} package(s)[/bold green]\n"
            )
            for pkg in upgraded:
                console.print(
                    f"  [dim]→[/dim] {pkg.name} {pkg.versions[0] if pkg.versions else ''}"
                )

            if current:
                console.print(f"\n[dim]{len(current)} already up-to-date:[/dim]\n")
                for pkg in current:
                    console.print(
                        f"  [dim]→ {pkg.name} {pkg.versions[0] if pkg.versions else ''}[/dim]"
                    )

            if failures:
                console.print(
                    f"\n[bold red]✗ {len(failures)} skipped/failed:[/bold red]"
                )
                for pkg_name, reason in failures:
                    console.print(f"  - {pkg_name}: [dim]{reason}[/dim]")

            console.print()

    except PinnedPackageWarning as e:
        console.print(f"\n[bold yellow]⚠ {e.message}[/bold yellow]\n")

    except Exception as e:
        sys.exit(handle_error(error=e))


def main(argv: list[str] | None = None) -> None:
    """Intercepts the entry point for the brewery CLI to handle commands passthrough."""
    if argv is None:
        argv = sys.argv[1:]

    # Pass unknown and non-flag arguments straight to brew
    if argv and not argv[0].startswith("-") and argv[0] not in KNOWN_COMMANDS:
        sys.exit(_brew_passthrough(argv))

    app()


if __name__ == "__main__":
    main()
