"""Derive package status from package info dictionary."""

from __future__ import annotations

from brewery.core.models import PackageKind, PackageStatus


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
