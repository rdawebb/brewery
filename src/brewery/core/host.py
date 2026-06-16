"""Host platform detection."""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass


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
