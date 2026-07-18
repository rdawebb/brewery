"""Cleanup command: remove old package versions."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console, run_async
from brewery.cli.error_formatting import CommandFailed, command_error
from brewery.cli.output import print_failures, print_result, spinner


@app.command(name="cleanup", aliases=["c", "clean"])
@command_error()
def cleanup() -> None:
    """Remove old package versions."""
    with _repository() as repo:
        sys.stdout.write("\n")
        with spinner("Cleaning up..."):
            removed, failures = run_async(coro=repo.cleanup_packages())

        if not removed and not failures:
            console.print("✓ Nothing to clean up\n", style="bold green")
            return

        print_result(
            f"✓ Removed {len(removed)} old version(s)\n", removed, style="bold green"
        )
        print_failures(f"\n✗ {len(failures)} could not be removed:", failures)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed
