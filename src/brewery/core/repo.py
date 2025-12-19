"""Repository module for managing package data from various backends."""

from __future__ import annotations

import time
from typing import List, Optional

from brewery.core.logging import get_logger
from brewery.core.models import Package, PackageKind
from brewery.providers import brew_cask, brew_formula

log = get_logger(__name__)


class Repository:
    """Repository for managing package data from various backends."""

    async def get_all_installed(self, kind_filter: Optional[PackageKind] = None) -> List[Package]:
        """Get all installed packages, optionally filtered by kind.

        Args:
            kind_filter: Optional filter for package kind (formula or cask).

        Returns:
            A list of installed Package instances.
        """
        start = time.perf_counter()
        log.info("fetch_packages_start", kind_filter=kind_filter.value if kind_filter else "all")

        pkgs: List[Package] = []

        if kind_filter in (None, PackageKind.FORMULA):
            pkgs.extend(await brew_formula.list_installed())
        if kind_filter in (None, PackageKind.CASK):
            pkgs.extend(await brew_cask.list_installed())

        pkgs.sort(key=lambda p: (p.kind.value, p.name.lower()))
        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "fetch_packages_complete",
            kind_filter=kind_filter.value if kind_filter else "all",
            count=len(pkgs),
            duration_ms=duration_ms,
        )

        return pkgs

    async def get_details(self, name: str, kind: PackageKind) -> Package:
        """Get package details by name and kind.

        Args:
            name: Name of the package.
            kind: Kind of the package (formula or cask).

        Returns:
            A Package instance with detailed information.
        """
        start = time.perf_counter()
        log.info("fetch_package_details_start", package=name, kind=kind.value)

        if kind is PackageKind.FORMULA:
            pkg = await brew_formula.info(name)
        else:
            pkg = await brew_cask.info(name)

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "fetch_package_details_complete", package=name, kind=kind.value, duration_ms=duration_ms
        )

        return pkg
