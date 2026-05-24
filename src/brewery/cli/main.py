"""CLI entry point for Brewery package management tool."""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Awaitable, List, Optional

from rich.console import Console
from typer_extensions import ExtendedTyper

from brewery.cli.renderers import (
    _terminal_size,
    package_details,
    package_table,
    paginate,
)
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
from brewery.core.task_manager import BackgroundTaskManager, get_task_manager
from brewery.daemon.daemon import daemon_app

log: BreweryLogger = get_logger(name=__name__)

app = ExtendedTyper(help="Brewery: A package management CLI tool")
app.add_typer(
    daemon_app,
    name="daemon",
    help="Daemon: Manage the Brewery background refresh daemon.",
)

console = Console(emoji=False, highlight=False)


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


def run_with_task_manager(coro: Awaitable) -> Any:
    """Run a coroutine with the task manager.

    Args:
        coro: The coroutine to run.

    Returns:
        The result of the coroutine.
    """

    async def main_with_tasks() -> None:
        result = await coro
        task_manager: BackgroundTaskManager = get_task_manager()
        await task_manager.wait_for_all()

        return result

    return asyncio.run(main=main_with_tasks())


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
    try:
        repo = Repository()
        pkgs: list[Package]

        if refresh:
            with console.status(
                status="[bold yellow]Refreshing cache...[/bold yellow]",
                refresh_per_second=6,
            ):
                pkgs = run_with_task_manager(
                    coro=repo.cache_mgr.refresh_packages(kind=kind)
                )
        else:
            pkgs = run_with_task_manager(coro=repo.get_all_installed(kind_filter=kind))

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
    try:
        repo = Repository()

        if kind is None:
            all_pkgs: List[Package] = run_with_task_manager(
                coro=repo.get_all_installed()
            )
            matching_pkg: Package | None = next(
                (p for p in all_pkgs if p.name == name), None
            )
            if not matching_pkg:
                raise PackageNotFoundError(package=name)
            resolved_kind: PackageKind = matching_pkg.kind

        pkg: Package = run_with_task_manager(coro=repo.get_details(name, resolved_kind))

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
        repo = Repository()
        pkgs: List[Package] = run_with_task_manager(coro=repo.get_all_installed())

        q: str = term.lower()
        pkgs = [
            p for p in pkgs if q in p.name.lower() or (p.desc and q in p.desc.lower())
        ]

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

        repo = Repository()
        with console.status(status="[bold green]Installing...", refresh_per_second=6):
            installed, failures = run_with_task_manager(
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

        repo = Repository()
        with console.status(
            status=f"[bold yellow]Uninstalling...{pkg_str}", refresh_per_second=6
        ):
            count, failures = run_with_task_manager(
                coro=repo.uninstall_packages(names, kind)
            )

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
        repo = Repository()
        pkgs: list[Package]

        if check:
            console.print()
            with console.status(
                status="[bold yellow]Checking for updates...[/bold yellow]",
                refresh_per_second=6,
            ):
                pkgs = run_with_task_manager(coro=repo.get_outdated(live=True))

        else:
            pkgs = run_with_task_manager(coro=repo.get_outdated(live=False))

        if not pkgs:
            console.print("\n[bold green]✓ All packages are up to date![/bold green]\n")
            return

        console.print(package_table(pkgs))
        console.print(
            f"\n[dim]{len(pkgs)} outdated package(s) - "
            f"Run [bold]brewery outdated --check[/bold] to refresh, "
            f"or [bold]brewery upgrade[/bold] to update all.[/dim]"
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
        repo = Repository()

        if not yes:
            if names:
                pkg_str: str = ", ".join(names)
                if not app.confirm(text=f"Upgrade: {pkg_str}?", default=True):
                    console.print("Upgrade cancelled.", style="dim")
                    return
            else:
                outdated: list[Package] = run_with_task_manager(
                    coro=repo.get_outdated(live=False)
                )
                if not outdated:
                    console.print(
                        "\n[bold green]✓ All packages are up to date![/bold green]\n"
                    )
                    return
                console.print(package_table(pkgs=outdated))
                if not app.confirm(
                    text=f"Upgrade {len(outdated)} outdated package(s)?", default=True
                ):
                    console.print("Upgrade cancelled.", style="dim")
                    return

        console.print()
        with console.status(
            status="[bold yellow]Upgrading...[/bold yellow]", refresh_per_second=6
        ):
            upgraded, current, failures = run_with_task_manager(
                coro=repo.upgrade_packages(names, kind)
            )

        if not upgraded and not failures:
            console.print("\n[bold green]✓ All packages are up to date![/bold green]\n")
            return

        console.print(
            f"[bold green]✓ Upgraded {len(upgraded)} package(s)[/bold green]\n"
        )
        for pkg in upgraded:
            console.print(
                f"  [dim]→[/dim] {pkg.name} {pkg.versions[0] if pkg.versions else ''}"
            )

        if current:
            console.print(f"\n[dim]{len(current)} already up-to-date:[/dim]")
            for pkg in current:
                console.print(
                    f"  [dim]→ {pkg.name} {pkg.versions[0] if pkg.versions else ''}[/dim]"
                )

        if failures:
            console.print(f"\n[bold red]✗ {len(failures)} skipped/failed:[/bold red]")
            for pkg_name, reason in failures:
                console.print(f"  - {pkg_name}: [dim]{reason}[/dim]")

    except PinnedPackageWarning as e:
        console.print(f"\n[bold yellow]⚠ {e.message}[/bold yellow]\n")
    except Exception as e:
        sys.exit(handle_error(error=e))


if __name__ == "__main__":
    app()
