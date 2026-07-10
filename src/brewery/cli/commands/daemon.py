"""Daemon commands: manage the brewery background refresh daemon."""

from __future__ import annotations

from typer_extensions import ExtendedTyper

from brewery.cli.context import console
from brewery.cli.error_formatting import command_error
from brewery.daemon import launchd
from brewery.daemon.launchd import PLIST_LABEL

daemon_app = ExtendedTyper(help="Manage the brewery background daemon.")


def _print_warnings(warnings: list[str]) -> None:
    """Surface advisories returned by the launchd layer.

    Args:
        warnings: Advisory messages, one per line.
    """
    for warning in warnings:
        console.print(f"\n{warning}\n", style="bold yellow")


@daemon_app.command(aliases=["a", "add"])
@command_error()
def start() -> None:
    """Activate the background daemon."""
    _print_warnings(launchd.start())
    console.print(
        f"\n✓ Daemon installed and loaded ({PLIST_LABEL})\n", style="bold green"
    )


@daemon_app.command(aliases=["d", "rm"])
@command_error()
def stop() -> None:
    """Deactivate the background daemon."""
    launchd.stop()
    console.print(f"\n✓ Daemon removed ({PLIST_LABEL})\n", style="bold green")


@daemon_app.command(aliases=["r"])
@command_error()
def restart() -> None:
    """Restart the background daemon."""
    launchd.stop()
    _print_warnings(launchd.start())
    console.print(f"\n✓ Daemon restarted ({PLIST_LABEL})\n", style="bold green")


@daemon_app.command(aliases=["st", "stat"])
@command_error()
def status() -> None:
    """Check whether the daemon is currently active."""
    if launchd.is_running():
        console.print(
            "\n✓ Background refresh + cleanup is active\n", style="bold green"
        )
        console.print(
            "  Use [bold]brewery daemon stop[/bold] to deactivate\n", style="dim"
        )

    else:
        console.print(
            "\n✗ Background refresh + cleanup is not active\n", style="bold red"
        )
        console.print(
            "  Use [bold]brewery daemon start[/bold] to activate\n", style="dim"
        )
