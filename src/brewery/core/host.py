"""Host platform detection."""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass

_SONOMA = 14
_BIG_SUR = 11


@dataclass(frozen=True, slots=True)
class Platform:
    """The current build platform for bottle selection."""

    arch: str  # "arm64" | "amd64"
    macos_major: int


def current_platform() -> Platform | None:
    """Detect the current macOS build platform, or None if not resolvable.

    Returns:
        Tuple of current (arch, OS major version), or None.
    """
    if _platform.system() != "Darwin":
        return None

    version: str = _platform.mac_ver()[0]
    if not version:
        return None

    try:
        major = int(version.split(".")[0])

    except ValueError:
        return None

    arch = "arm64" if _platform.machine() == "arm64" else "amd64"

    return Platform(arch=arch, macos_major=major)


def preferred_perl_version(macos_major: int | None = None) -> str:
    """The system perl for this macOS.

    Args:
        macos_major: The host's macOS major version; detected when omitted.

    Returns:
        The system perl version string.
    """
    if macos_major is None:
        plat = current_platform()
        macos_major = plat.macos_major if plat else _SONOMA

    if macos_major >= _SONOMA:
        return "5.34"

    if macos_major >= _BIG_SUR:
        return "5.30"

    return "5.18"
