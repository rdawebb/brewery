"""Repository module for managing package data from various backends."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Literal, Optional

from structlog.typing import FilteringBoundLogger

if TYPE_CHECKING:
    from ty_extensions import Unknown

from brewery.core.cache import Cache
from brewery.core.errors import BrewCommandError, CacheError, PackageNotFoundError
from brewery.core.logging import get_logger
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.core.task_manager import TaskManager, get_task_manager
from brewery.providers import brew_cask, brew_formula, brew_outdated

log: FilteringBoundLogger = get_logger(name=__name__)


class Repository:
    """Repository for managing package data from various backends."""

    def __init__(self):
        """Initialise the repository."""
        self.cache = Cache(namespace="repository")

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
        tasks = []

        if kind_filter in (None, PackageKind.FORMULA):
            tasks.append(brew_formula.list_installed())
        if kind_filter in (None, PackageKind.CASK):
            tasks.append(brew_cask.list_installed())

        if tasks:
            results = await asyncio.gather(*tasks)
        for result in results:
            pkgs.extend(result)

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

        pkgs: list[Package] = await self._fetch_pkgs(kind_filter)
        pkgs_dicts: list[dict[str, Any]] = [p.to_serializable_dict() for p in pkgs]
        mapping: dict[Any, dict[str, Any]] = {p["name"]: p for p in pkgs_dicts}

        try:
            self.cache.set(key=cache_key, value=pkgs_dicts)
            self.cache.set(key=map_key, value=mapping)
        except CacheError as e:
            log.error(event="Failed to refresh cache", error=str(object=e))

        if return_pkgs:
            return pkgs
        if return_map:
            return mapping

        return None

    async def _invalidate_and_refresh(self, kind: PackageKind) -> None:
        """Invalidate the cache & map for given kind, and re-fetch

        Args:
            kind: The kind of package (formula or cask) to invalidate.
        """
        for suffix in [kind.value, "all"]:
            for prefix in ("installed_", "installed_map_"):
                key = f"{prefix}{suffix}"
                f: Unknown | Path = self.cache._file(key)
                if f.exists():
                    f.unlink()
                    log.info(event="cache_invalidated", key=key)

        await self._refresh_cache(kind_filter=kind)
        await self._refresh_cache(kind_filter=None)  # Refresh the 'all' cache as well

    async def _update_cache(
        self,
        name: str,
        kind: PackageKind,
        action: Literal["add", "remove", "update"],
        pkg: Optional[Package] = None,
    ) -> None:
        """Update single cache record in place.

        Args:
            name: The name of the package.
            kind: The kind of package (formula or cask).
            action: The action to perform (add, remove, or update).
            pkg: The package instance to add or update (required for add/update actions).
        """
        for suffix in [kind.value, "all"]:
            list_key = f"installed_{suffix}"
            map_key = f"installed_map_{suffix}"

            try:
                cached_list = self.cache.get(key=list_key)
                cached_map = self.cache.get(key=map_key)

                if cached_list is None or cached_map is None:
                    log.warning(
                        event="cache_update_failed_missing_cache",
                        list_key=list_key,
                        map_key=map_key,
                    )
                    continue

                if action == "remove":
                    cached_map.pop(name, None)
                    cached_list = [p for p in cached_list if p.get("name") != name]

                elif action in ("add", "update") and pkg:
                    pkg_dict = pkg.to_serializable_dict()
                    cached_map[name] = pkg_dict

                    cached_list = [p for p in cached_list if p.get("name") != name]
                    cached_list.append(pkg_dict)

                    cached_list.sort(
                        key=lambda p: (p.get("kind", ""), p.get("name", "").lower())
                    )

                self.cache.set(key=list_key, value=cached_list)
                self.cache.set(key=map_key, value=cached_map)
                log.info(
                    event="cache_updated",
                    list_key=list_key,
                    map_key=map_key,
                )

            except CacheError as e:
                log.error(event="cache_update_failed", error=str(object=e))

    async def _batch_update_cache(
        self, packages: list[Package], kinds: list[PackageKind]
    ) -> None:
        """Update multiple cache records in place

        Args:
            packages: The list of packages
        """
        for kind in kinds:
            list_key = f"installed_{kind.value}"
            map_key = f"installed_map_{kind.value}"

            cached_list = self.cache.get(key=list_key)
            cached_map = self.cache.get(key=map_key)

            if cached_list is None or cached_map is None:
                continue

            for pkg in packages:
                if kind == "all" or pkg.kind == kind:
                    pkg_dict: dict[str, Any] = pkg.to_serializable_dict()
                    cached_map[pkg.name] = pkg_dict
                    cached_list = [
                        pkg for pkg in cached_list if pkg.get("name", "") != pkg.name
                    ]
                    cached_list.append(pkg_dict)

            cached_list.sort(
                key=lambda p: (p.get("kind", ""), p.get("name", "").lower())
            )

            self.cache.set(key=list_key, value=cached_list)
            self.cache.set(key=map_key, value=cached_map)

    async def get_all_installed(
        self, kind_filter: Optional[PackageKind] = None
    ) -> List[Package]:
        """Get all installed packages, optionally filtered by kind.

        Args:
            kind_filter: Optional filter for package kind (formula or cask).

        Returns:
            A list of installed Package instances.
        """
        start: int | float = time.perf_counter()
        log.info(
            event="fetch_packages_start",
            kind_filter=kind_filter.value if kind_filter else "all",
        )

        cache_key = f"installed_{kind_filter.value if kind_filter else 'all'}"

        try:
            cached_data: Unknown | None = self.cache.get(key=cache_key)

            if cached_data is not None and not cached_data == []:
                pkgs: list[Package] = [
                    Package.package_from_dict(data=d) for d in cached_data
                ]
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.info(
                    event="fetch_packages_complete",
                    kind_filter=kind_filter.value if kind_filter else "all",
                    count=len(pkgs),
                    duration_ms=duration_ms,
                )

                return pkgs

            else:
                pkgs: (
                    list[Unknown] | dict[Unknown, Unknown] | None
                ) = await self._refresh_cache(kind_filter, return_pkgs=True)

        except CacheError as e:
            log.error(
                event="Package list cache error", error=str(object=e), key=cache_key
            )

            pkgs: (
                list[Unknown] | dict[Unknown, Unknown] | None
            ) = await self._refresh_cache(kind_filter, return_pkgs=True)

            if not pkgs:
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.warning(
                    event="No packages found after cache error and refresh",
                    kind_filter=kind_filter.value if kind_filter else "all",
                    duration_ms=duration_ms,
                )
                return []

        if isinstance(pkgs, list):
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                event="fetch_packages_complete",
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
        start: int | float = time.perf_counter()
        log.info(event="fetch_package_details_start", package=name, kind=kind.value)

        map_key = f"installed_map_{kind.value}"
        cache_key = f"installed_{kind.value}"

        try:
            cached_data: Unknown | None = self.cache.get(key=map_key)
            if cached_data is not None and not cached_data == []:
                return Package.package_from_dict(data=cached_data[name])
        except (CacheError, KeyError) as e:
            log.error(
                event="Package details mapping cache error",
                error=str(object=e),
                key=map_key,
            )

        # Fallback to list cache
        try:
            pkg_list: Unknown | None = self.cache.get(key=cache_key)
            if pkg_list is not None and not pkg_list == []:
                for pkg in pkg_list:
                    if pkg.get("name") == name:
                        duration_ms = int((time.perf_counter() - start) * 1000)
                        log.info(
                            event="fetch_package_details_complete",
                            package=name,
                            kind=kind.value,
                            duration_ms=duration_ms,
                        )
                        return Package.package_from_dict(data=pkg)
        except CacheError as e:
            log.error(
                event="Package details list cache error",
                error=str(object=e),
                key=cache_key,
            )

        # Fallback to refresh cache
        try:
            mapping: (
                list[Unknown] | dict[Unknown, Unknown] | None
            ) = await self._refresh_cache(kind_filter=kind, return_map=True)
            if isinstance(mapping, dict) and name in mapping:
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.info(
                    event="fetch_package_details_complete",
                    package=name,
                    kind=kind.value,
                    duration_ms=duration_ms,
                )
                return Package.package_from_dict(data=mapping[name])
        except CacheError as e:
            log.error(
                event="Package details refresh cache error",
                error=str(object=e),
                key=map_key,
            )

        # Check backend if not in cache
        try:
            if kind is PackageKind.FORMULA:
                pkg: Package = await brew_formula.info(name)
            else:
                pkg: Package = await brew_cask.info(name)
        except Exception as e:
            log.error(
                event="Package details fetch error",
                error=str(object=e),
                package=name,
                kind=kind.value,
            )
            raise PackageNotFoundError(package=name, kind=kind.value) from e

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="fetch_package_details_complete",
            package=name,
            kind=kind.value,
            duration_ms=duration_ms,
        )

        if not pkg:
            raise PackageNotFoundError(package=name, kind=kind.value)

        return pkg

    async def install_package(self, name: str, kind: PackageKind) -> Package:
        """Install a package and refresh cache on success.

        Args:
            name: Name of the package to install.
            kind: Kind of the package (formula or cask).

        Returns:
            The package details on success.

        Raises:
            BrewCommandError: Propagated from provider.
        """
        start: float = time.perf_counter()
        log.info(event="install_package_start", package=name, kind=kind.value)

        if kind is PackageKind.FORMULA:
            await brew_formula.install(name)
            pkg: Package = await brew_formula.info(name)
        else:
            await brew_cask.install(name)
            pkg: Package = await brew_cask.info(name)

        await self._update_cache(name=name, kind=kind, action="add", pkg=pkg)

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="install_package_complete",
            package=name,
            kind=kind.value,
            duration_ms=duration_ms,
        )

        return pkg

    async def uninstall_package(self, name: str, kind: PackageKind) -> None:
        """Uninstall a package and refresh cache on success.

        Args:
            name: Name of the package to uninstall.
            kind: Kind of the package (formula or cask).

        Returns:
            None

        Raises:
            BrewCommandError: Propagated from provider.
        """
        start: float = time.perf_counter()
        log.info(event="uninstall_package_start", package=name, kind=kind.value)

        if kind is PackageKind.FORMULA:
            await brew_formula.uninstall(name)
        else:
            await brew_cask.uninstall(name)

        await self._update_cache(name=name, kind=kind, action="remove")

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="uninstall_package_complete",
            package=name,
            kind=kind.value,
            duration_ms=duration_ms,
        )

    async def get_outdated(self, live: bool = False) -> list[Package]:
        """Return a list of outdated packages.

        Args:
            live: If True, call brew directly and refresh cache, otherwise use cached data.

        Returns:
            List of packages with OUTDATED status.
        """
        start: float = time.perf_counter()
        log.info(event="get_outdated_start", live=live)

        if live:
            task_manager: TaskManager = get_task_manager()
            task_manager.add_task(coro=self._refresh_outdated_status())

            outdated_entries: list[
                dict[Unknown, Unknown]
            ] = await brew_outdated.fetch_outdated()

            outdated_pkgs: list[Package] = [
                Package.package_from_dict(data=entry) for entry in outdated_entries
            ]

            duration_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                event="outdated_live_fetch_complete",
                count=len(outdated_pkgs),
                duration_ms=duration_ms,
            )

            return outdated_pkgs

        pkgs: list[Package] = await self.get_all_installed()

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="outdated_cache_fetch_complete",
            count=len(pkgs),
            duration_ms=duration_ms,
        )

        return [pkg for pkg in pkgs if pkg.status == PackageStatus.OUTDATED]

    async def _refresh_outdated_status(self) -> None:
        """Refresh the status of outdated packages in the background."""
        try:
            log.info(event="outdated_cache_refresh_start_background")
            outdated_entries: list[
                dict[Unknown, Unknown]
            ] = await brew_outdated.fetch_outdated()
            outdated_map: dict[Unknown, dict[Unknown, Unknown]] = {
                entry["name"]: entry for entry in outdated_entries
            }

            all_pkgs: list[Package] = await self.get_all_installed()

            updated_pkgs: list[Package] = []
            for pkg in all_pkgs:
                if pkg.name in outdated_map:
                    entry: dict[Unknown, Unknown] = outdated_map[pkg.name]
                    pkg.status |= PackageStatus.OUTDATED
                    if pkg.metadata:
                        pkg.metadata["latest_version"] = entry.get("current_version")
                updated_pkgs.append(pkg)

            cache_key = "installed_all"
            pkgs_dicts: list[dict[str, Any]] = [
                pkg.to_serializable_dict() for pkg in updated_pkgs
            ]
            self.cache.set(key=cache_key, value=pkgs_dicts)
            log.info(
                event="outdated_cache_updated_background", count=len(outdated_entries)
            )

        except Exception as e:
            log.error(event="outdated_background_refresh_failed", error=str(object=e))

    async def upgrade_package(self, name: str, kind: PackageKind) -> Package:
        """Upgrade a single package and refresh cache entry.

        Args:
            name: Name of the package to upgrade.
            kind: Kind of the package (formula or cask).

        Returns:
            The upgraded package details.

        Raises:
            BrewCommandError: Propagated from provider.
            PackagePinnedWarning: If the package is pinned.
        """
        if kind is PackageKind.FORMULA:
            await brew_formula.upgrade(name)
            pkg: Package = await brew_formula.info(name)
        else:
            await brew_cask.upgrade(name)
            pkg: Package = await brew_cask.info(name)

        await self._update_cache(name=name, kind=kind, action="update", pkg=pkg)

        return pkg

    async def upgrade_all_outdated(self) -> tuple[list[Package], list[tuple[str, str]]]:
        """Upgrade all outdated packages.

        Returns:
            A tuple containing a list of upgraded packages and a list of failures.
        """
        outdated: list[Package] = await self.get_outdated(live=False)
        upgraded: list[Package] = []
        failures: list[tuple[str, str]] = []
        kind_map: dict[str, str] = {}

        formulas_to_upgrade: list[Package] = [
            p
            for p in outdated
            if p.kind == PackageKind.FORMULA and PackageStatus.PINNED not in p.status
        ]
        for pkg in formulas_to_upgrade:
            kind_map[pkg.name] = "brew_formula"

        casks_to_upgrade: list[Package] = [
            p
            for p in outdated
            if p.kind == PackageKind.CASK and PackageStatus.PINNED not in p.status
        ]
        for pkg in casks_to_upgrade:
            kind_map[pkg.name] = "brew_cask"

        for pkg in outdated:
            if PackageStatus.PINNED in pkg.status:
                log.info(event="upgrade_skipped_pinned", package=pkg.name)
                failures.append((pkg.name, "pinned - skipped"))

        async def _upgrade_batch(packages: list[Package], provider) -> list[str]:
            if not packages:
                return []

            names: list[str] = [pkg.name for pkg in packages]

            try:
                await provider.upgrade(names)
                return names

            except BrewCommandError as e:
                log.error(
                    event="batch_upgrade_failed",
                    error=str(object=e),
                )
                for pkg in packages:
                    failures.append((pkg.name, str(object=e.message)))
                return []

        success_formula_names: list[str] = await _upgrade_batch(
            packages=formulas_to_upgrade,
            provider=brew_formula,
        )
        success_cask_names: list[str] = await _upgrade_batch(
            packages=casks_to_upgrade, provider=brew_cask
        )

        success_names: list[str] = success_formula_names + success_cask_names
        if not success_names:
            return [], failures

        info_tasks: list = []
        for name in success_names:
            kind = kind_map[name]
            provider = brew_formula if kind == PackageKind.FORMULA else brew_cask
            info_tasks.append(provider.info(name))

        upgraded_pkgs: list[Package] = list(await asyncio.gather(*info_tasks))

        upgraded_kinds: list[PackageKind] = []
        if success_formula_names:
            upgraded_kinds.append(PackageKind.FORMULA)
        if success_cask_names:
            upgraded_kinds.append(PackageKind.CASK)

        await self._batch_update_cache(packages=upgraded_pkgs, kinds=upgraded_kinds)

        log.info(
            event="upgrade_all_outdated_complete",
            upgraded=len(upgraded),
            failed=len(failures),
        )

        return upgraded, failures
