"""Repository module for managing package data from various backends."""

from __future__ import annotations

import time
from typing import List, Optional

from brewery.core.errors import CacheError, PackageNotFoundError
from brewery.core.cache import Cache
from brewery.core.logging import get_logger
from brewery.core.models import Package, PackageKind
from brewery.providers import brew_cask, brew_formula

log = get_logger(__name__)


class Repository:
    """Repository for managing package data from various backends."""

    def __init__(self):
        self.cache = Cache("repository")

    async def _fetch_pkgs(
        self, kind_filter: Optional[PackageKind] = None
    ) -> List[Package]:
        """Fetch all installed packages, optionally filtered by kind, with caching.

        Args:
            kind_filter: Optional filter for package kind (formula or cask).

        Returns:
            A list of installed Package instances.
        """
        pkgs: List[Package] = []
        if kind_filter in (None, PackageKind.FORMULA):
            pkgs.extend(await brew_formula.list_installed())
        if kind_filter in (None, PackageKind.CASK):
            pkgs.extend(await brew_cask.list_installed())
        pkgs.sort(key=lambda p: (p.kind.value, p.name.lower()))

        return pkgs

    async def _refresh_cache(
        self,
        kind_filter: Optional[PackageKind] = None,
        return_pkgs: bool = False,
        return_map: bool = False,
    ) -> Optional[list | dict]:
        """Refresh the package cache.

        Args:
            kind_filter: Optional filter for package kind (formula or cask).
        """
        cache_key = f"installed_{kind_filter.value if kind_filter else 'all'}"
        map_key = f"installed_map_{kind_filter.value if kind_filter else 'all'}"

        pkgs = await self._fetch_pkgs(kind_filter)
        pkgs_dicts = [p.to_serializable_dict() for p in pkgs]
        mapping = {p["name"]: p for p in pkgs_dicts}

        try:
            self.cache.set(cache_key, pkgs_dicts)
            self.cache.set(map_key, mapping)
        except CacheError as e:
            log.error("Failed to refresh cache", error=str(e))

        if return_pkgs:
            return pkgs
        if return_map:
            return mapping

        return None

    async def get_all_installed(
        self, kind_filter: Optional[PackageKind] = None
    ) -> List[Package]:
        """Get all installed packages, optionally filtered by kind.

        Args:
            kind_filter: Optional filter for package kind (formula or cask).

        Returns:
            A list of installed Package instances.
        """
        start = time.perf_counter()
        log.info(
            "fetch_packages_start",
            kind_filter=kind_filter.value if kind_filter else "all",
        )

        cache_key = f"installed_{kind_filter.value if kind_filter else 'all'}"

        try:
            print(f"Before cache lookup: {(time.perf_counter() - start) * 1000:.2f} ms")
            cached_data = self.cache.get(cache_key)
            print(f"After cache lookup: {(time.perf_counter() - start) * 1000:.2f} ms")
            if cached_data is not None and not cached_data == []:
                print(
                    f"Before deserialization: {(time.perf_counter() - start) * 1000:.2f} ms"
                )
                pkgs = [Package.package_from_dict(d) for d in cached_data]
                print(
                    f"After deserialization: {(time.perf_counter() - start) * 1000:.2f} ms"
                )
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.info(
                    "fetch_packages_complete",
                    kind_filter=kind_filter.value if kind_filter else "all",
                    count=len(pkgs),
                    duration_ms=duration_ms,
                )
                return pkgs

            else:
                pkgs = await self._refresh_cache(kind_filter, return_pkgs=True)

        except CacheError as e:
            log.error("Package list cache error", error=str(e), key=cache_key)

            pkgs = await self._refresh_cache(kind_filter, return_pkgs=True)

            if not pkgs:
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.warning(
                    "No packages found after cache error and refresh",
                    kind_filter=kind_filter.value if kind_filter else "all",
                    duration_ms=duration_ms,
                )
                return []

        if isinstance(pkgs, list):
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                "fetch_packages_complete",
                kind_filter=kind_filter.value if kind_filter else "all",
                count=len(pkgs),
                duration_ms=duration_ms,
            )

            return pkgs

        return []

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

        map_key = f"installed_map_{kind.value}"
        cache_key = f"installed_{kind.value}"

        try:
            cached_data = self.cache.get(map_key)
            if cached_data is not None and not cached_data == []:
                return Package.package_from_dict(cached_data[name])
        except CacheError as e:
            log.error("Package details mapping cache error", error=str(e), key=map_key)

        # Fallback to list cache
        try:
            pkg_list = self.cache.get(cache_key)
            if pkg_list is not None and not pkg_list == []:
                for pkg in pkg_list:
                    if pkg.get("name") == name:
                        duration_ms = int((time.perf_counter() - start) * 1000)
                        log.info(
                            "fetch_package_details_complete",
                            package=name,
                            kind=kind.value,
                            duration_ms=duration_ms,
                        )
                        return Package.package_from_dict(pkg)
        except CacheError as e:
            log.error("Package details list cache error", error=str(e), key=cache_key)

        # Fallback to refresh cache
        try:
            mapping = await self._refresh_cache(kind, return_map=True)
            if isinstance(mapping, dict) and name in mapping:
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.info(
                    "fetch_package_details_complete",
                    package=name,
                    kind=kind.value,
                    duration_ms=duration_ms,
                )
                return Package.package_from_dict(mapping[name])
        except CacheError as e:
            log.error("Package details refresh cache error", error=str(e), key=map_key)

        # Check backend if not in cache
        try:
            if kind is PackageKind.FORMULA:
                pkg = await brew_formula.info(name)
            else:
                pkg = await brew_cask.info(name)
        except Exception as e:
            log.error(
                "Package details fetch error",
                error=str(e),
                package=name,
                kind=kind.value,
            )
            raise PackageNotFoundError(package=name, kind=kind.value) from e

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "fetch_package_details_complete",
            package=name,
            kind=kind.value,
            duration_ms=duration_ms,
        )

        if not pkg:
            raise PackageNotFoundError(package=name, kind=kind.value)

        return pkg
