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
    print_caveats,
    print_failures,
    print_result,
    sudo_prompts,
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
        formula: Install the package(s) as formulae.
        cask: Install the package(s) as casks; with neither flag, each name is
            a formula if one exists, else a cask.
        yes: If true, skip confirmation prompt.
    """
    target: PackageKind | None = kind_from_flags(formula, cask)
    label = f"{target.value}: " if target else ""

    if not confirm_or_cancel(
        f"Install {label}{', '.join(names)}?", yes=yes, default=True
    ):
        return

    with _repository() as repo:
        reporter = make_reporter(console)
        with sudo_prompts(reporter):
            installed, failures, caveats = run_async(
                coro=install_packages(repo, names, target, progress=reporter)
            )

        print_result(
            f"✓ Installed {len(installed)} package(s)\n",
            map(pkg_line, installed),
            style="bold green",
            bullet="→",
        )

        print_failures(f"✗ Failed to install {len(failures)} package(s)", failures)

        print_caveats(caveats)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed
