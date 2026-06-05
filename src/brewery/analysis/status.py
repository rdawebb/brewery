"""Derive package status from package info dictionary."""

from __future__ import annotations

from typing import Any, TypedDict

from brewery.core.models import PackageKind, PackageStatus

InstalledFormula = list[dict[str, Any]]
InstalledCask = str


class StatusInfo(TypedDict, total=False):
    """TypedDict for package status information."""

    outdated: bool | None
    version: dict
    pinned: bool | None
    keg_only: bool | None
    linked_keg: str | None
    installed: InstalledFormula | InstalledCask | None
    service: dict | None


def derive_status(
    info: StatusInfo, kind: PackageKind = PackageKind.FORMULA
) -> PackageStatus:
    """Derive the PackageStatus from package info dictionary.

    Legacy path via `brew info --json=v2` output.

    Args:
        info (dict): The package info dictionary.

    Returns:
        PackageStatus: The derived package status.
    """
    status: PackageStatus = PackageStatus.NONE

    if info.get("outdated") or info.get("version", {}).get("outdated"):
        status |= PackageStatus.OUTDATED
    if info.get("pinned") is True:
        status |= PackageStatus.PINNED
    service = info.get("service")
    if isinstance(service, dict) and service:
        status |= PackageStatus.HAS_SERVICE

    if kind == PackageKind.FORMULA:
        if info.get("keg_only") is True:
            status |= PackageStatus.KEG_ONLY
        if info.get("linked_keg") in (None, "") and info.get("installed"):
            status |= PackageStatus.NOT_LINKED

    return status


def derive_local_status(
    *,
    kind: PackageKind,
    head: bool = False,
    linked: bool = True,
    pinned: bool = False,
) -> PackageStatus:
    """Derive the filesystem-knowable half of a package's status.

    Returns only the flags the installed state can answer via filesystem state.
    Keyword-only by design: the three flags are all booleans.

    Args:
        kind: The package kind. Local flags apply to formulae only.
        head: Whether the active keg is a HEAD build.
        linked: Whether the formula is linked into the prefix. Defaults to True
            so that a formula is not falsely flagged `NOT_LINKED`.
        pinned: Whether the formula is pinned.

    Returns:
        The locally-derived PackageStatus.
    """
    status: PackageStatus = PackageStatus.NONE

    if kind == PackageKind.FORMULA:
        if pinned:
            status |= PackageStatus.PINNED
        if head:
            status |= PackageStatus.HEAD
        if not linked:
            status |= PackageStatus.NOT_LINKED

    return status
