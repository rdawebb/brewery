"""CLI entry point for Brewery package management tool."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Coroutine, Optional

from rich.console import Console
from typer_extensions import ExtendedTyper

from brewery.cli.error_formatting import format_error_message, suggest_search
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
)
from brewery.core.logging import BreweryLogger, configure_logging, get_logger
from brewery.core.models import Package, PackageKind
from brewery.core.repo import Repository
from brewery.core.shell import BrewOutput, run_brew
from brewery.daemon.daemon import daemon_app

log: BreweryLogger = get_logger(name=__name__)

app = ExtendedTyper(help="Brewery: A package management CLI tool")
app.add_typer(
    daemon_app,
    name="daemon",
    aliases=["d"],
    help="Manage the Brewery background refresh daemon.",
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
    "d",
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
        console.print("\n✗ brew not found on PATH\n", style="bold red")
        return EXIT_SYSTEM_ERROR

    try:
        import asyncio

        return asyncio.run(
            run_brew(argv, output=BrewOutput.INHERIT, check=False, timeout=None)
        ).returncode

    except FileNotFoundError:
        console.print("\n✗ brew not found\n", style="bold red")
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
    import asyncio

    return asyncio.run(coro)


@app.callback()
def setup() -> None:
    """Set up the CLI environment"""
    configure_logging(level="INFO", enable_console=True)


@app.command(name="list", aliases=["ls", "l"])
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
                    refresh_per_second=5,
                ):
                    repo.cache_mgr.invalidate()
                    pkgs = repo.get_all_installed(kind_filter=kind)
            else:
                pkgs = repo.get_all_installed(kind_filter=kind)

            _, term_height = _terminal_size()
            page_size: int = term_height - 6  # header + footer buffer

            if len(pkgs) > page_size:
                paginate(pkgs=pkgs, page_size=page_size, console=console)
            else:
                console.print(package_table(pkgs), emoji=False)

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command(aliases=["i", "in"])
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
            pkg: Package = repo.get_details(name, kind)

            console.print(package_details(pkg))

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command(aliases=["s", "find"])
def search(term: str) -> None:
    """Search for packages by name or description.

    Args:
        term: Search term.
    """
    try:
        with _repository() as repo:
            pkgs: list[Package] = repo.search(term)
            from brewery.cli.renderers import package_table

            console.print(package_table(pkgs))

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command(aliases=["add"])
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
                console.print("\nInstallation cancelled\n", style="dim")
                return

        with _repository() as repo:
            app.echo()
            with console.status(
                status="[bold green]Installing...[/bold green]", refresh_per_second=5
            ):
                installed, failures = _async_run(
                    coro=repo.install_packages(names, kind)
                )

            console.print(
                f"✓ Installed {len(installed)} package(s)\n", style="bold green"
            )
            for pkg in installed:
                console.print(
                    f"  [dim]→[/dim] {pkg.name} {pkg.versions[0] if pkg.versions else ''}"
                )

            if failures:
                console.print(
                    f"✗ Failed to install {len(failures)} package(s)", style="bold red"
                )
                for name, reason in failures:
                    console.print(f"  [dim]-[/dim] {name} - {reason}")

            app.echo()

    except AlreadyInstalledWarning as e:
        console.print(f"\n⚠ {e.message}\n", style="bold yellow")

    except KeyboardInterrupt:
        console.print(
            "\n⚠ Interrupted. Re-run [bold]brewery install <name>[/bold] to complete it\n",
            style="bold yellow",
        )
        sys.exit(130)

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command(aliases=["rm", "remove"])
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
                console.print("\nUninstallation cancelled\n", style="dim")
                return

        with _repository() as repo:
            app.echo()
            with console.status(
                status="[bold yellow]Uninstalling...[/bold yellow]",
                refresh_per_second=5,
            ):
                removed, failures = _async_run(
                    coro=repo.uninstall_packages(names, kind)
                )

            console.print(
                f"✓ Uninstalled {len(removed)} package(s)\n", style="bold green"
            )
            for pkg in removed:
                console.print(f"  [dim]-[/dim] {pkg}")

            if failures:
                console.print(
                    f"✗ Failed to uninstall {len(failures)} package(s)",
                    style="bold red",
                )
                for name, reason in failures:
                    console.print(f"  [dim]-[/dim] {name} - {reason}")

            app.echo()

    except KeyboardInterrupt:
        console.print(
            "\n⚠ Interrupted. Re-run [bold]brewery uninstall <name>[/bold] to complete it\n",
            style="bold yellow",
        )
        sys.exit(130)

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command(aliases=["o", "out"])
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
                app.echo()
                with console.status(
                    status="[bold yellow]Checking for updates...[/bold yellow]",
                    refresh_per_second=5,
                ):
                    from brewery.daemon.catalog_refresh import refresh_catalog

                    _async_run(coro=refresh_catalog(catalog=repo.catalog))
                    repo.cache_mgr.invalidate()
                    pkgs = repo.get_outdated()

            else:
                pkgs = repo.get_outdated()

            if not pkgs:
                console.print("\n✓ All packages are up to date!\n", style="bold green")
                return

            console.print(f"\n• {len(pkgs)} outdated package(s)\n", style="bold yellow")
            for pkg in pkgs:
                latest = pkg.metadata.get("latest_version")
                console.print(f"  [dim]-[/dim] {pkg.name} → {latest}")

            console.print(
                "\n  Run [bold]brewery upgrade[/bold] to update all outdated packages, "
                "\n  or [bold]brewery upgrade <packages>[/bold] to update specific packages\n",
                style="dim",
            )

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command(aliases=["u", "up"])
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
                    outdated: list[Package] = repo.get_outdated()
                    if not outdated:
                        console.print(
                            "\n✓ All packages are up to date!\n", style="bold green"
                        )
                        return

                    console.print(
                        f"\n• {len(outdated)} outdated package(s)\n",
                        style="bold yellow",
                    )
                    for pkg in outdated:
                        latest = pkg.metadata.get("latest_version")
                        console.print(f"  [dim]-[/dim] {pkg.name} → {latest}")

                    app.echo()
                    if not app.confirm(
                        text=f"Upgrade {len(outdated)} outdated package(s)?",
                        default=True,
                    ):
                        console.print("Upgrade cancelled.", style="dim")
                        return

            app.echo()
            with console.status(
                status="[bold yellow]Upgrading...[/bold yellow]", refresh_per_second=5
            ):
                upgraded, current, failures = _async_run(
                    coro=repo.upgrade_packages(names, kind)
                )

            if not upgraded and not failures and not current:
                console.print("✓ All packages are up to date!\n", style="bold green")
                return

            console.print(
                f"✓ Upgraded {len(upgraded)} package(s)\n", style="bold green"
            )
            for pkg in upgraded:
                console.print(
                    f"  [dim]→[/dim] {pkg.name} {pkg.versions[0] if pkg.versions else ''}"
                )

            if current:
                console.print(f"\n{len(current)} already up-to-date:\n", style="dim")
                for pkg in current:
                    console.print(
                        f"  - {pkg.name} {pkg.versions[0] if pkg.versions else ''}",
                        style="dim",
                    )

            if failures:
                console.print(f"\n✗ {len(failures)} skipped/failed:", style="bold red")
                for pkg_name, reason in failures:
                    console.print(f"  - {pkg_name}: [dim]{reason}[/dim]")

            app.echo()

    except PinnedPackageWarning as e:
        console.print(f"\n[bold yellow]⚠ {e.message}[/bold yellow]\n")

    except KeyboardInterrupt:
        console.print(
            "\n⚠ Interrupted. Re-run [bold]brewery upgrade <name>[/bold] to complete it\n",
            style="bold yellow",
        )
        sys.exit(130)

    except Exception as e:
        sys.exit(handle_error(error=e))


def main(argv: list[str] | None = None) -> None:
    """Intercepts the entry point for the brewery CLI to handle commands passthrough.

    Args:
        argv: The command-line arguments to pass to the brewery CLI.
    """
    if argv is None:
        argv = sys.argv[1:]

    # Pass unknown and non-flag arguments straight to brew
    if argv and not argv[0].startswith("-") and argv[0] not in KNOWN_COMMANDS:
        sys.exit(_brew_passthrough(argv))

    app()


if __name__ == "__main__":
    main()
