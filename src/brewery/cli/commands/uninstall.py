"""Uninstall command: uninstall a package or list of packages."""

from __future__ import annotations

import sys
from typing import Annotated

from brewery.cli.context import (
    CaskFlag,
    FormulaFlag,
    _repository,
    app,
    kind_from_flags,
    run_async,
)
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
    formula: FormulaFlag = False,
    cask: CaskFlag = False,
    yes: Annotated[
        bool, app.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
) -> None:
    """Uninstall a package or list of packages.

    Args:
        names: Name(s) of the package(s) to uninstall.
        formula: Treat the package(s) as formulae.
        cask: Treat the package(s) as casks. With neither flag, auto-detects.
        yes: If true, skip confirmation prompt.
    """
    kind: PackageKind | None = kind_from_flags(formula, cask)

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
