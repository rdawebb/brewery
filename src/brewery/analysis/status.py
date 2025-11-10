"""Derive package status from package info dictionary."""

from __future__ import annotations

from brewery.core.models import PackageStatus


def derive_status(info: dict) -> PackageStatus:
    """Derive the PackageStatus from package info dictionary."""
    status = PackageStatus.NONE

    if info.get("outdated") or info.get("version", {}).get("outdated"):
        status |= PackageStatus.OUTDATED
    if info.get("pinned") is True:
        status |= PackageStatus.PINNED
    if info.get("keg_only") is True:
        status |= PackageStatus.KEG_ONLY
    if info.get("linked_keg") in (None, "") and info.get("installed"):
        status |= PackageStatus.NOT_LINKED
    if any(s.get("service") for s in info.get("service", []) if isinstance(s, dict)):
        status |= PackageStatus.HAS_SERVICE

    return status 