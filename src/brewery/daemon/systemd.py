"""systemd (user) service management for the brewery background daemon."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from brewery.core.errors import SysError, UserError

SERVICE_LABEL = "brewery-daemon"
SERVICE_NAME = f"{SERVICE_LABEL}.service"
TIMER_NAME = f"{SERVICE_LABEL}.timer"


def _unit_dir() -> Path:
    """Resolve the systemd user unit directory (honours XDG_CONFIG_HOME).

    Returns:
        The `$XDG_CONFIG_HOME/systemd/user` directory (or the ~/.config default).
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"

    return base / "systemd" / "user"


def _template(name: str) -> str:
    """Read a bundled unit template from the packaged scripts directory.

    Args:
        name: The unit file name (e.g. `brewery-daemon.service`).

    Returns:
        The template text.
    """
    import importlib.resources

    return importlib.resources.files("brewery.scripts").joinpath(name).read_text()


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run a `systemctl --user` subcommand, capturing output.

    Args:
        *args: The systemctl arguments after `--user`.

    Returns:
        The completed process.
    """
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True
    )


def render_units() -> tuple[str, str, list[str]]:
    """Render the .service and .timer unit text from current settings.

    Every resolvable field is filled even when one lookup fails, so a missing
    brew never leaves a unit carrying a stale interval or an unresolved
    interpreter.

    Returns:
        (service_text, timer_text, warnings): the rendered units and advisory
        messages for the user (empty when everything resolved).
    """
    from brewery.core.settings import load_settings

    warnings: list[str] = []

    python = sys.executable or shutil.which("python3")
    if not python:
        warnings.append("Could not locate a python interpreter — daemon may not work")
        python = "python3"

    brew = shutil.which("brew")
    if brew:
        path = f"{Path(brew).parent}:/usr/local/bin:/usr/bin:/bin"

    else:
        warnings.append("Could not locate brew on PATH — daemon may not work")
        path = "/usr/local/bin:/usr/bin:/bin"

    interval_sec = load_settings().daemon.catalog_refresh_interval_mins * 60

    service = _template(SERVICE_NAME).format(python=python, path=path)
    timer = _template(TIMER_NAME).format(interval_sec=interval_sec)

    return service, timer, warnings


def _install_units() -> list[str]:
    """Render and write the unit files into the user unit directory.

    Returns:
        Advisory messages raised while rendering the units.
    """
    service, timer, warnings = render_units()

    unit_dir = _unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / SERVICE_NAME).write_text(service)
    (unit_dir / TIMER_NAME).write_text(timer)

    return warnings


def is_running() -> bool:
    """Report whether the daemon timer is active.

    Returns:
        True if `systemctl --user is-active <timer>` succeeds.
    """
    return _systemctl("is-active", TIMER_NAME).returncode == 0


def start() -> list[str]:
    """Install the units and enable+start the timer.

    Returns:
        Advisory messages raised while rendering the units.

    Raises:
        SysError: If `systemctl --user enable --now` fails.
    """
    warnings = _install_units()

    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", TIMER_NAME)
    if result.returncode != 0:
        raise SysError(
            "systemctl enable failed",
            context={"returncode": result.returncode, "stderr": result.stderr.strip()},
        )

    return warnings


def stop() -> None:
    """Disable+stop the timer and remove its unit files.

    Raises:
        UserError: If the daemon is not installed.
    """
    unit_dir = _unit_dir()
    if not (unit_dir / TIMER_NAME).exists():
        raise UserError("Daemon is not installed")

    _systemctl("disable", "--now", TIMER_NAME)
    (unit_dir / TIMER_NAME).unlink(missing_ok=True)
    (unit_dir / SERVICE_NAME).unlink(missing_ok=True)
    _systemctl("daemon-reload")
