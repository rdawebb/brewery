"""Host platform detection."""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass

_SONOMA = 14
_BIG_SUR = 11

# Machine names reporting a 64-bit ARM host: Darwin is "arm64", Linux
# "aarch64"; both map to brew's single `arm64` token
ARM_MACHINES = frozenset({"arm64", "aarch64"})


@dataclass(frozen=True, slots=True)
class Platform:
    """The current build platform for bottle selection."""

    arch: str  # "arm64" | "amd64"
    os: str  # "macos" | "linux"
    macos_major: int | None = None  # None on Linux


def _detect_arch() -> str:
    """Map the host machine name to a bottle arch token.

    Returns:
        "arm64" for Apple Silicon / aarch64, else "amd64".
    """
    return "arm64" if _platform.machine() in ARM_MACHINES else "amd64"


def current_platform() -> Platform | None:
    """Detect the current build platform, or None if not resolvable.

    Returns:
        The current Platform (macOS or Linux), or None.
    """
    system = _platform.system()

    if system == "Linux":
        return Platform(arch=_detect_arch(), os="linux")

    if system != "Darwin":
        return None

    version: str = _platform.mac_ver()[0]
    if not version:
        return None

    try:
        major = int(version.split(".")[0])

    except ValueError:
        return None

    return Platform(arch=_detect_arch(), os="macos", macos_major=major)


def preferred_perl_version(macos_major: int | None = None) -> str:
    """The system perl for this macOS.

    Args:
        macos_major: The host's macOS major version; detected when omitted.

    Returns:
        The system perl version string.
    """
    if macos_major is None:
        plat = current_platform()
        macos_major = plat.macos_major if plat and plat.macos_major else _SONOMA

    if macos_major >= _SONOMA:
        return "5.34"

    if macos_major >= _BIG_SUR:
        return "5.30"

    return "5.18"
