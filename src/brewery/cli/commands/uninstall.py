"""Uninstall command: uninstall a package or list of packages."""

from __future__ import annotations

import sys
from typing import Annotated

from brewery.cli.context import _repository, app, run_async
from brewery.cli.error_formatting import CommandFailed, command_error
from brewery.cli.output import (
    confirm_or_cancel,
    print_failures,
    print_result,
    spinner,
)
from brewery.core.models import PackageKind
from brewery.services.uninstall import uninstall_packages


@app.command(aliases=["rm", "del"])
@command_error(interrupt_hint="brewery uninstall <name>")
def uninstall(
    names: list[str],
    kind: Annotated[
        PackageKind | None, app.Option("--kind", help="formula | cask | auto (default)")
    ] = None,
    yes: Annotated[
        bool, app.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
) -> None:
    """Uninstall a package or list of packages.

    Args:
        names: Name(s) of the package(s) to uninstall.
        kind: Kind of the package(s) (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    if not confirm_or_cancel(
        f"Uninstall: {', '.join(names)}?", yes=yes, default=False, style="bold yellow"
    ):
        return

    with _repository() as repo:
        with spinner("Uninstalling..."):
            removed, failures = run_async(coro=uninstall_packages(repo, names, kind))

        print_result(
            f"✓ Uninstalled {len(removed)} package(s)\n", removed, style="bold green"
        )
        print_failures(f"✗ Failed to uninstall {len(failures)} package(s)", failures)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed
