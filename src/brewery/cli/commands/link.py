"""Link and unlink commands: manage a formula's symlinks in the prefix."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from brewery.cli.context import _repository, app
from brewery.cli.error_formatting import CommandFailed, command_error
from brewery.cli.output import (
    print_advisories,
    print_failures,
    print_result,
)

if TYPE_CHECKING:
    from brewery.providers.linker import LinkResult


def _preview_link(linked: list[tuple[str, LinkResult]]) -> None:
    """Print the paths a real `link` would create and, with --overwrite, delete.

    Args:
        linked: The (name, result) pairs reported by the repository.
    """
    for name, result in linked:
        print_result(
            f"Would link {len(result.linked)} path(s) for {name}:",
            result.linked,
            style="bold",
        )

        if result.conflicts:
            print_result(
                f"\nWould delete {len(result.conflicts)} existing path(s):",
                [dst for dst, _ in result.conflicts],
                style="bold yellow",
            )


@app.command(aliases=["ln"])
@command_error()
def link(
    names: list[str],
    overwrite: bool = app.Option(
        False, "--overwrite", help="Delete files that already exist in the prefix"
    ),
    dry_run: bool = app.Option(
        False, "--dry-run", "-n", help="List what would be linked or deleted"
    ),
    force: bool = app.Option(
        False, "--force", "-f", help="Allow keg-only formulae to be linked"
    ),
) -> None:
    """Symlink a formula's installed files into the prefix.

    Args:
        names: Name(s) of the formula(e) to link.
        overwrite: Delete conflicting prefix files while linking.
        dry_run: Report what would be linked without touching the filesystem.
        force: Allow keg-only formulae to be linked.
    """
    with _repository() as repo:
        sys.stdout.write("\n")
        linked, advisories, failures = repo.link_packages(
            names, overwrite=overwrite, force=force, dry_run=dry_run
        )

        print_advisories(advisories)

        if dry_run:
            _preview_link(linked)

        elif linked or not failures:
            print_result(
                f"✓ Linked {len(linked)} package(s)\n",
                [
                    f"{name} - created {len(result.linked)} symlink(s)"
                    for name, result in linked
                ],
                style="bold green",
            )

        print_failures(f"✗ Failed to link {len(failures)} package(s)", failures)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed


@app.command(aliases=["ul"])
@command_error()
def unlink(
    names: list[str],
    dry_run: bool = app.Option(
        False, "--dry-run", "-n", help="List what would be unlinked"
    ),
) -> None:
    """Remove a formula's symlinks from the prefix.

    Args:
        names: Name(s) of the formula(e) to unlink.
        dry_run: Report what would be removed without touching the filesystem.
    """
    with _repository() as repo:
        sys.stdout.write("\n")
        unlinked, advisories, failures = repo.unlink_packages(names, dry_run=dry_run)

        print_advisories(advisories)

        if dry_run:
            for name, result in unlinked:
                print_result(
                    f"Would remove {len(result.removed)} path(s) for {name}:",
                    result.removed,
                    style="bold",
                )

        elif unlinked or not failures:
            print_result(
                f"✓ Unlinked {len(unlinked)} package(s)\n",
                [
                    f"{name} - removed {len(result.removed)} symlink(s)"
                    for name, result in unlinked
                ],
                style="bold green",
            )

        print_failures(f"✗ Failed to unlink {len(failures)} package(s)", failures)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed
