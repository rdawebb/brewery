"""Decide which casks brewery can install natively and which go to brew whole."""

from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any, Literal

from brewery.core.catalog import CaskRow
from brewery.core.host import Platform

Route = Literal["native", "brew"]

# Artifact stanzas brewery can stage itself (`target` annotates an `app`/`binary`)
NATIVE_ARTIFACTS = frozenset(
    {
        "app",
        "binary",
        "font",
        "suite",
        "qlplugin",
        "artifact",
        "manpage",
        "bash_completion",
        "zsh_completion",
        "fish_completion",
        "uninstall",
        "zap",
        "target",
    }
)

NATIVE_UNINSTALL = frozenset({"quit", "delete", "launchctl", "signal"})

_ARCH_TYPES = {"arm": "arm64", "intel": "amd64"}

_COMPARE: dict[str, Callable[[int, int], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}


def classify(row: CaskRow, plat: Platform | None) -> tuple[Route, str]:
    """Route a cask to the native installer or to brew, with the reason.

    Args:
        row: The cask's catalog row.
        plat: The host platform, from `current_platform()`.

    Returns:
        Tuple of (route, reason). The reason is empty for a native cask.
    """
    if plat is None or plat.os != "macos" or plat.macos_major is None:
        return "brew", "native casks need macOS"

    for stanza in row.artifacts or []:
        if not isinstance(stanza, dict):
            return "brew", "unrecognised artifact"

        foreign = sorted(stanza.keys() - NATIVE_ARTIFACTS)
        if foreign:
            return "brew", f"`{foreign[0]}` artifact"

        for directive in stanza.get("uninstall", []):
            foreign = sorted(directive.keys() - NATIVE_UNINSTALL)
            if foreign:
                return "brew", f"`uninstall {foreign[0]}`"

    return _check_depends(row.depends_on or {}, plat.macos_major, plat.arch)


def _check_depends(
    depends_on: dict[str, Any], macos_major: int, arch: str
) -> tuple[Route, str]:
    """Hand casks this host can't run to brew, which refuses with its own message.

    `formula` and `cask` dependencies are resolved by the installer, not here.

    Args:
        depends_on: The cask's decoded `depends_on`.
        macos_major: The host's macOS major version.
        arch: The host's arch token.

    Returns:
        Tuple of (route, reason).
    """
    for key in ("macos", "maximum_macos"):
        for op, versions in (depends_on.get(key) or {}).items():
            # The host is known only to its major version, so compare majors
            majors = [int(v.split(".")[0]) for v in versions]
            ok = (
                macos_major in majors
                if op == "=="
                else _COMPARE[op](macos_major, majors[0])
            )
            if not ok:
                return "brew", f"needs macOS {op} {', '.join(versions)}"

    archs = {_ARCH_TYPES.get(a.get("type")) for a in depends_on.get("arch") or []}
    if archs and arch not in archs:
        return "brew", f"needs {'/'.join(sorted(a for a in archs if a))}"

    return "native", ""
