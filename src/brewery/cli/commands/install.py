"""Install command: install a package or list of packages."""

from __future__ import annotations

import sys
from typing import Annotated

from brewery.cli.context import (
    CaskFlag,
    FormulaFlag,
    _repository,
    app,
    console,
    kind_from_flags,
    run_async,
)
from brewery.cli.error_formatting import CommandFailed, command_error
from brewery.cli.output import (
    confirm_or_cancel,
    pkg_line,
    print_failures,
    print_result,
)
from brewery.cli.progress import make_reporter
from brewery.core.errors import AlreadyInstalledWarning
from brewery.core.models import PackageKind
from brewery.services.install import install_packages


@app.command(aliases=["add"])
@command_error(
    warnings=(AlreadyInstalledWarning,), interrupt_hint="brewery install <name>"
)
def install(
    names: Annotated[list[str], app.Argument()],
    formula: FormulaFlag = False,
    cask: CaskFlag = False,
    yes: Annotated[
        bool, app.Option("--yes", "-y", help="Skip confirmation prompt")
    ] = False,
) -> None:
    """Install a package or list of packages.

    Args:
        names: Name(s) of the package(s) to install.
        formula: Install the package(s) as formulae (the default).
        cask: Install the package(s) as casks.
        yes: If true, skip confirmation prompt.
    """
    target: PackageKind = kind_from_flags(formula, cask) or PackageKind.FORMULA

    if not confirm_or_cancel(
        f"Install {target.value}: {', '.join(names)}?", yes=yes, default=True
    ):
        return

    with _repository() as repo:
        installed, failures = run_async(
            coro=install_packages(repo, names, target, progress=make_reporter(console))
        )

        print_result(
            f"✓ Installed {len(installed)} package(s)\n",
            map(pkg_line, installed),
            style="bold green",
            bullet="→",
        )
        print_failures(f"✗ Failed to install {len(failures)} package(s)", failures)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed
