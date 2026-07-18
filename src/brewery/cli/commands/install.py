"""Install command: install a package or list of packages."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console, run_async
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


@app.command(aliases=["add"])
@command_error(
    warnings=(AlreadyInstalledWarning,), interrupt_hint="brewery install <name>"
)
def install(
    names: list[str] = app.Argument(...),
    kind: PackageKind | None = app.Option(
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
    target: PackageKind = kind or PackageKind.FORMULA

    if not confirm_or_cancel(
        f"Install {target.value}: {', '.join(names)}?", yes=yes, default=True
    ):
        return

    with _repository() as repo:
        installed, failures = run_async(
            coro=repo.install_packages(names, target, progress=make_reporter(console))
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
