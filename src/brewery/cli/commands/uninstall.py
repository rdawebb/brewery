"""Uninstall command: uninstall a package or list of packages."""

from __future__ import annotations

from brewery.cli.context import _repository, app, run_async
from brewery.cli.error_formatting import CommandFailed, command_error
from brewery.cli.output import (
    confirm_or_cancel,
    print_failures,
    print_result,
    spinner,
)
from brewery.core.models import PackageKind


@app.command(aliases=["rm", "del"])
@command_error(interrupt_hint="brewery uninstall <name>")
def uninstall(
    names: list[str],
    kind: PackageKind | None = app.Option(
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
    if not confirm_or_cancel(f"Uninstall: {', '.join(names)}?", yes=yes, default=False):
        return

    with _repository() as repo:
        app.echo()
        with spinner("Uninstalling..."):
            removed, failures = run_async(coro=repo.uninstall_packages(names, kind))

        print_result(
            f"✓ Uninstalled {len(removed)} package(s)\n", removed, style="bold green"
        )
        print_failures(f"✗ Failed to uninstall {len(failures)} package(s)", failures)

        app.echo()
        if failures:
            raise CommandFailed
