"""Daemon CLI sub app for managing the brewery background daemon."""

import shutil
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from typer_extensions import ExtendedTyper

PLIST_LABEL = "com.brewery.refresh"
PLIST_NAME = f"{PLIST_LABEL}.plist"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST_DEST = LAUNCH_AGENTS / PLIST_NAME

daemon_app = ExtendedTyper(help="Manage the brewery background refresh daemon.")

console = Console(emoji=False, highlight=False)


def _plist_source() -> Path:
    """Resolve the bundled plist path.

    Returns:
        The path to the bundled plist file.
    """
    import importlib.resources

    ref = importlib.resources.files("brewery.scripts").joinpath(PLIST_NAME)
    with importlib.resources.as_file(ref) as p:
        return Path(p)


def _patch_executable_paths(plist_path: Path) -> None:
    """Rewrite the Python interpreter and brew paths for the current Homebrew prefix.

    Args:
        plist_path: The path to the plist file to patch.
    """
    python = shutil.which("python3") or sys.executable
    brew = shutil.which("brew")
    if not brew:
        console.print(
            "\nCould not locate brew on PATH — daemon may not work\n",
            style="bold yellow",
        )
        return

    import plistlib

    data = plistlib.loads(plist_path.read_bytes())

    args = data.get("ProgramArguments", [])
    if args:
        args[0] = python

    data.setdefault("EnvironmentVariables", {})["PATH"] = (
        f"{Path(brew).parent}:/usr/local/bin:/usr/bin:/bin"
    )

    plist_path.write_bytes(plistlib.dumps(data))


@daemon_app.command_with_aliases(aliases=["a", "add"])
def start(
    force: bool = daemon_app.Option(
        False, "--force", "-f", help="Overwrite existing daemon file."
    ),
) -> None:
    """Activate the background refresh daemon.

    Args:
        force: Whether to overwrite an existing plist file.
    """
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    if PLIST_DEST.exists() and not force:
        console.print(
            f"\nDaemon already installed at {PLIST_DEST}. Use --force to reinstall.",
            style="bold yellow",
        )
        sys.exit(1)

    shutil.copy2(_plist_source(), PLIST_DEST)
    _patch_executable_paths(PLIST_DEST)

    result = subprocess.run(["launchctl", "load", "-w", str(PLIST_DEST)])
    if result.returncode != 0:
        console.print("launchctl load failed.", style="bold red")
        sys.exit(result.returncode)

    console.print(
        f"\n✓ Daemon installed and loaded ({PLIST_LABEL})\n", style="bold green"
    )


@daemon_app.command_with_aliases(aliases=["d", "rm"])
def stop() -> None:
    """Deactivate the background refresh daemon."""
    if not PLIST_DEST.exists():
        console.print("\nDaemon is not installed\n", style="bold yellow")
        sys.exit(1)

    subprocess.run(["launchctl", "unload", "-w", str(PLIST_DEST)])
    PLIST_DEST.unlink()
    console.print(f"\n✓ Daemon removed ({PLIST_LABEL})\n", style="bold green")


@daemon_app.command_with_aliases(aliases=["r"])
def restart() -> None:
    """Restart the background refresh daemon."""
    stop()
    start()


@daemon_app.command_with_aliases(aliases=["st", "stat"])
def status() -> None:
    """Check whether the daemon is currently active."""
    result = subprocess.run(
        ["launchctl", "list", PLIST_LABEL],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        console.print("\n✓ Background refresh is active", style="bold green")
        console.print("- Use `brewery daemon stop` to deactivate\n", style="dim")

    else:
        console.print("\n✗ Background refresh is not active", style="bold red")
        console.print("- Use `brewery daemon start` to activate\n", style="dim")
