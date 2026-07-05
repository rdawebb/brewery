"""Config CLI subcommand for viewing/changing brewery configuration and settings."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console
from typer_extensions import Context, ExtendedTyper

config_app = ExtendedTyper(help="View brewery configuration and settings.")

console = Console(emoji=False, highlight=False)


def _fmt_count(value: int | None, unit: str) -> str:
    """Format a count value as 'unlimited' or 'N unit(s)'.

    Args:
        value: The count value to format.
        unit: The unit of measure (e.g. 'formula', 'cellar MB').

    Returns:
        The formatted count string.
    """
    if value is None:
        return "unlimited"

    return f"{value} {unit}" + ("" if value == 1 else "s")


def _config_status(path: Path) -> tuple[str, bool]:
    """Classify the on-disk config for display. Returns (message, ok).

    Args:
        path: The path to the config file.

    Returns:
        A tuple containing the status message and a boolean indicating success.
    """
    import orjson

    if not path.exists():
        return "not found — using defaults", True

    try:
        raw = orjson.loads(path.read_bytes())

    except (OSError, orjson.JSONDecodeError):
        return "invalid JSON — using defaults", False

    if not isinstance(raw, dict):
        return "not a JSON object — using defaults", False

    return "loaded", True


def _render_config() -> None:
    """Print the resolved settings and active retention policy."""
    from brewery.core.config import get_config_dir
    from brewery.core.settings import CONFIG_NAME, load_settings

    path = get_config_dir() / CONFIG_NAME
    status, ok = _config_status(path)
    s = load_settings()

    config_app.echo()
    console.print("[bold]Configuration[/bold]\n")
    console.print(f"  File   {path}")
    console.print(f"         {status}", style="dim" if ok else "bold yellow")

    console.print("\n[bold]Retention[/bold]")
    console.print(f"  Age threshold     {_fmt_count(s.retention.age_days, 'day')}")
    console.print(
        f"  Max versions      {_fmt_count(s.retention.max_versions, 'version')}"
        + ("" if s.retention.max_versions is None else " per formula")
    )
    console.print(
        "  Max Cellar size   "
        + (
            "unlimited"
            if s.retention.max_cellar_mb is None
            else f"{s.retention.max_cellar_mb} MB"
        )
    )

    console.print("\n[bold]Daemon[/bold]")
    console.print(
        f"  Cleanup interval  {_fmt_count(s.daemon.cleanup_interval_days, 'day')}"
    )

    console.print("\n[bold]Display[/bold]")
    console.print(f"  Format            {s.display.format}")
    config_app.echo()


@config_app.callback(invoke_without_command=True)
def _config_default(ctx: Context) -> None:
    """Show config when `brewery config` is run with no subcommand."""
    if ctx.invoked_subcommand is None:
        _render_config()


@config_app.command(name="show", aliases=["s"])
def config_show() -> None:
    """Show the resolved configuration and active retention policy."""
    _render_config()


@config_app.command(name="path", aliases=["p"])
def config_path() -> None:
    """Print the config file path (created on first `config set`, or edit by hand)."""
    from brewery.core.config import get_config_dir
    from brewery.core.settings import CONFIG_NAME

    print(get_config_dir() / CONFIG_NAME)  # Bare stdout, pipe/editor friendly


@config_app.command(name="set")
def config_set(
    key: str = config_app.Argument(..., help="Dotted key, e.g. retention.age_days"),
    value: str = config_app.Argument(
        ..., help="New value ('unlimited' disables a cap)"
    ),
) -> None:
    """Set a configuration value."""
    from brewery.cli.error_formatting import handle_error
    from brewery.core.errors import BrewError
    from brewery.core.settings import write_setting

    try:
        written = write_setting(key, value)

    except BrewError as e:
        sys.exit(handle_error(error=e))

    shown = "unlimited" if written is None else written
    console.print(f"\n✓ Set [bold]{key}[/bold] to {shown}\n", style="bold green")


@config_app.command(name="get")
def config_get(
    key: str = config_app.Argument(..., help="Dotted key, e.g. retention.age_days"),
) -> None:
    """Print one configuration value (resolved, with defaults applied)."""
    from brewery.cli.error_formatting import handle_error
    from brewery.core.errors import BrewError
    from brewery.core.settings import get_setting

    try:
        value = get_setting(key)

    except BrewError as e:
        sys.exit(handle_error(error=e))

    print("unlimited" if value is None else value)  # Bare stdout, pipe-friendly
