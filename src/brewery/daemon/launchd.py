"""launchd service management for the brewery background daemon."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from brewery.core.errors import SysError, UserError

PLIST_LABEL = "com.brewery.daemon"
PLIST_NAME = f"{PLIST_LABEL}.plist"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST_DEST = LAUNCH_AGENTS / PLIST_NAME

# Backend-neutral label used by the CLI and the daemon dispatch layer
SERVICE_LABEL = PLIST_LABEL


def _gui_domain() -> str:
    """Return the launchd GUI domain target for the current user.

    Returns:
        The GUI domain for the current user.
    """
    return f"gui/{os.getuid()}"


def _service_target() -> str:
    """Return the fully-qualified service target for the daemon.

    Returns:
        The service target for the daemon.
    """
    return f"{_gui_domain()}/{PLIST_LABEL}"


def _plist_source() -> Path:
    """Resolve the bundled plist path.

    Returns:
        The path to the bundled plist file.
    """
    import importlib.resources

    ref = importlib.resources.files("brewery.scripts").joinpath(PLIST_NAME)
    with importlib.resources.as_file(ref) as p:
        return Path(p)


def is_running() -> bool:
    """Report whether launchd currently has the daemon bootstrapped.

    Returns:
        True if the service target is known to launchd.
    """
    return (
        subprocess.run(
            ["launchctl", "print", _service_target()],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def patch_plist(plist_path: Path) -> list[str]:
    """Rewrite the plist with paths and interval from current settings.

    Every resolvable key is patched even when one lookup fails, so a missing brew
    never leaves the plist carrying a stale interval or an unresolved interpreter.

    Args:
        plist_path: The path to the plist file to patch.

    Returns:
        Advisory messages for the user; empty when the plist was fully patched.
    """
    import plistlib

    from brewery.core.logging import ensure_log_dir
    from brewery.core.settings import load_settings

    warnings: list[str] = []
    data = plistlib.loads(plist_path.read_bytes())

    python = sys.executable or shutil.which("python3")
    args = data.get("ProgramArguments", [])
    if not python:
        warnings.append("Could not locate a python interpreter — daemon may not work")

    elif args:
        args[0] = python

    brew = shutil.which("brew")
    if brew:
        data.setdefault("EnvironmentVariables", {})["PATH"] = (
            f"{Path(brew).parent}:/usr/local/bin:/usr/bin:/bin"
        )

    else:
        warnings.append("Could not locate brew on PATH — daemon may not work")

    interval_mins = load_settings().daemon.catalog_refresh_interval_mins
    data["StartInterval"] = interval_mins * 60

    # launchd does not expand '~', so these must be absolute
    log_dir = ensure_log_dir()
    data["StandardErrorPath"] = str(log_dir / "refresh.err")
    data["StandardOutPath"] = str(log_dir / "refresh.out")

    plist_path.write_bytes(plistlib.dumps(data))

    return warnings


def start() -> list[str]:
    """Install the plist and bootstrap the daemon into launchd.

    Returns:
        Advisory messages raised while patching the plist.

    Raises:
        SysError: If `launchctl bootstrap` fails.
    """
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)

    if is_running():
        subprocess.run(["launchctl", "bootout", _service_target()], check=False)

    shutil.copy2(_plist_source(), PLIST_DEST)
    warnings = patch_plist(PLIST_DEST)

    result = subprocess.run(
        ["launchctl", "bootstrap", _gui_domain(), str(PLIST_DEST)], check=False
    )
    if result.returncode != 0:
        raise SysError(
            "launchctl bootstrap failed",
            context={"returncode": result.returncode},
        )

    return warnings


def stop() -> None:
    """Bootout the daemon and remove its plist.

    Raises:
        UserError: If the daemon is not installed.
    """
    if not PLIST_DEST.exists():
        raise UserError("Daemon is not installed")

    subprocess.run(["launchctl", "bootout", _service_target()], check=False)
    PLIST_DEST.unlink()
