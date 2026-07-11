"""Outdated command: list outdated packages."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console, run_async
from brewery.cli.error_formatting import command_error
from brewery.cli.output import print_result, spinner
from brewery.core.models import Package


@app.command(aliases=["o", "out"])
@command_error()
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
    with _repository() as repo:
        pkgs: list[Package]

        sys.stdout.write("\n")
        if check:
            with spinner("Checking for updates..."):
                from brewery.daemon.catalog_refresh import refresh_catalog

                run_async(coro=refresh_catalog(catalog=repo.catalog))
                repo.cache_mgr.invalidate()
                pkgs = repo.get_outdated()

        else:
            pkgs = repo.get_outdated()

        if not pkgs:
            console.print("✓ All packages are up to date!\n", style="bold green")
            return

        print_result(
            f"• {len(pkgs)} outdated package(s)\n",
            (f"{pkg.name} → {pkg.metadata.get('latest_version')}" for pkg in pkgs),
            style="bold yellow",
        )

        console.print(
            "\n  Run [bold]brewery upgrade[/bold] to update all outdated packages, "
            "\n  or [bold]brewery upgrade <packages>[/bold] to update specific packages\n",
            style="dim",
        )
