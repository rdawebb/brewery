"""Upgrade command: upgrade one, list, or all outdated packages."""

from __future__ import annotations

import sys
from typing import Annotated

from brewery.cli.context import _repository, app, console, run_async
from brewery.cli.error_formatting import CommandFailed, command_error
from brewery.cli.output import (
    confirm_or_cancel,
    pkg_line,
    print_advisories,
    print_failures,
    print_result,
)
from brewery.cli.progress import make_reporter
from brewery.core.errors import PinnedPackageWarning
from brewery.core.models import Package, PackageKind
from brewery.services.upgrade import upgrade_packages


@app.command(aliases=["u", "up"])
@command_error(
    warnings=(PinnedPackageWarning,), interrupt_hint="brewery upgrade <name>"
)
def upgrade(
    names: Annotated[
        list[str] | None,
        app.Argument(help="Package(s) to upgrade (leave empty to upgrade all)"),
    ] = None,
    kind: Annotated[
        PackageKind | None, app.Option("--kind", help="formula | cask | auto (default)")
    ] = None,
    yes: Annotated[
        bool, app.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
) -> None:
    """Upgrade one, list, or all outdated packages.

    Args:
        names: Name(s) of the package(s) to upgrade (if None, upgrades all outdated).
        kind: Kind of the package (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    with _repository() as repo:
        if yes:
            sys.stdout.write("\n")

        elif names:
            if not confirm_or_cancel(f"Upgrade: {', '.join(names)}?", yes=False):
                return

        else:
            outdated: list[Package] = repo.get_outdated()
            if not outdated:
                console.print("\n✓ All packages are up to date!\n", style="bold green")
                return

            print_result(
                f"\n• {len(outdated)} outdated package(s)\n",
                (
                    f"{pkg.name} → {pkg.metadata.get('latest_version')}"
                    for pkg in outdated
                ),
                style="bold yellow",
            )

            if not confirm_or_cancel(
                f"Upgrade {len(outdated)} outdated package(s)?", yes=False
            ):
                return

        upgraded, current, advisories, failures = run_async(
            coro=upgrade_packages(repo, names, kind, progress=make_reporter(console))
        )

        if not upgraded and not advisories and not failures and not current:
            console.print("✓ All packages are up to date!\n", style="bold green")
            return

        print_advisories(advisories)

        print_result(
            f"✓ Upgraded {len(upgraded)} package(s)\n",
            map(pkg_line, upgraded),
            style="bold green",
            bullet="→",
        )

        if current:
            print_result(
                f"\n{len(current)} already up-to-date:\n",
                map(pkg_line, current),
                style="dim",
                line_style="dim",
            )

        print_failures(f"\n✗ {len(failures)} failed:", failures)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed
