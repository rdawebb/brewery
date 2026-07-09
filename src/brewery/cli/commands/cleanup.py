"""Cleanup command: remove old package versions."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console, run_async
from brewery.cli.error_formatting import handle_error


@app.command(name="cleanup", aliases=["c", "clean"])
def cleanup() -> None:
    """Remove old package versions."""
    try:
        with _repository() as repo:
            app.echo()
            with console.status(
                status="[bold yellow]Cleaning up...[/bold yellow]", refresh_per_second=5
            ):
                removed, failures = run_async(coro=repo.cleanup_packages())

            if not removed and not failures:
                console.print("✓ Nothing to clean up\n", style="bold green")
                return

            console.print(
                f"✓ Removed {len(removed)} old version(s)\n", style="bold green"
            )
            for label in removed:
                console.print(f"  [dim]-[/dim] {label}")

            if failures:
                console.print(
                    f"\n✗ {len(failures)} could not be removed:", style="bold red"
                )
                for label, reason in failures:
                    console.print(f"  [dim]-[/dim] {label} - {reason}")

            app.echo()

    except Exception as e:
        sys.exit(handle_error(error=e))
