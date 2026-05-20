"""Derive package status from package info dictionary."""

from __future__ import annotations

from typing import Any, TypedDict

from brewery.core.models import PackageStatus

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


def derive_status(info: StatusInfo) -> PackageStatus:
    """Derive the PackageStatus from package info dictionary.

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
    if info.get("keg_only") is True:
        status |= PackageStatus.KEG_ONLY
    if info.get("linked_keg") in (None, "") and info.get("installed"):
        status |= PackageStatus.NOT_LINKED
    service = info.get("service")
    if isinstance(service, dict) and service:
        status |= PackageStatus.HAS_SERVICE

    return status
