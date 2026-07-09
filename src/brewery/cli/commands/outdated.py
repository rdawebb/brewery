"""Outdated command: list outdated packages."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console, run_async
from brewery.cli.error_formatting import handle_error
from brewery.core.models import Package


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

            app.echo()
            if check:
                with console.status(
                    status="[bold yellow]Checking for updates...[/bold yellow]",
                    refresh_per_second=5,
                ):
                    from brewery.daemon.catalog_refresh import refresh_catalog

                    run_async(coro=refresh_catalog(catalog=repo.catalog))
                    repo.cache_mgr.invalidate()
                    pkgs = repo.get_outdated()

            else:
                pkgs = repo.get_outdated()

            if not pkgs:
                console.print("✓ All packages are up to date!\n", style="bold green")
                return

            console.print(f"• {len(pkgs)} outdated package(s)\n", style="bold yellow")
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
