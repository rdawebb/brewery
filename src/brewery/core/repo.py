"""Repository module for managing package data from various backends."""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional

from structlog.typing import FilteringBoundLogger

from brewery.core.cache import Cache, CacheManager
from brewery.core.errors import BrewCommandError, PackageNotFoundError
from brewery.core.logging import get_logger
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.core.task_manager import BackgroundTaskManager, get_task_manager
from brewery.providers import brew_cask, brew_formula, brew_outdated

log: FilteringBoundLogger = get_logger(name=__name__)


class Repository:
    """Repository for managing package data from various backends."""

    def __init__(self):
        """Initialise the repository."""
        self.cache = Cache(namespace="repository")
        self.cache_mgr = CacheManager(self.cache)

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

        pkgs: list[Package] = await self.cache_mgr.load_packages(kind=kind_filter)
        if not pkgs:
            pkgs: list[Package] = await self.cache_mgr.refresh_packages(
                kind=kind_filter
            )

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="get_all_installed_complete", count=len(pkgs), duration_ms=duration_ms
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
        start: int | float = time.perf_counter()
        log.info(event="fetch_package_details_start", package=name, kind=kind.value)

        pkg: Package | None = await self.cache_mgr.get_details_from_cache(name, kind)
        if pkg:
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                event="get_details_complete", package=name, duration_ms=duration_ms
            )
            return pkg

        if kind == PackageKind.FORMULA:
            pkg: Package = await brew_formula.info(name)
        else:
            pkg: Package = await brew_cask.info(name)

        if not pkg:
            raise PackageNotFoundError(package=name, kind=kind.value)

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(event="get_details_complete", package=name, duration_ms=duration_ms)

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

        await self.cache_mgr.update_single(name=name, kind=kind, action="add", pkg=pkg)

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

        await self.cache_mgr.update_single(name=name, kind=kind, action="remove")

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
            # Background task to refresh cache
            task_mgr: BackgroundTaskManager = get_task_manager()
            task_mgr.add_task(coro=self.cache_mgr.refresh_outdated_status())

            # Fetch live outdated data
            outdated_entries: list = await brew_outdated.fetch_outdated()
            outdated_pkgs: list[Package] = [
                Package.package_from_dict(data=e) for e in outdated_entries
            ]
        else:
            # Use cached data
            all_pkgs: list[Package] = await self.get_all_installed()
            outdated_pkgs: list[Package] = [
                p for p in all_pkgs if PackageStatus.OUTDATED in p.status
            ]

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="get_outdated_complete",
            count=len(outdated_pkgs),
            duration_ms=duration_ms,
        )
        return outdated_pkgs

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

        await self.cache_mgr.update_single(
            name=name, kind=kind, action="update", pkg=pkg
        )

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
            kind: str = kind_map[name]
            provider = brew_formula if kind == PackageKind.FORMULA else brew_cask
            info_tasks.append(provider.info(name))

        upgraded_pkgs: list[Package] = list(await asyncio.gather(*info_tasks))

        upgraded_kinds: list[PackageKind] = []
        if success_formula_names:
            upgraded_kinds.append(PackageKind.FORMULA)
        if success_cask_names:
            upgraded_kinds.append(PackageKind.CASK)

        await self.cache_mgr.update_batch(packages=upgraded_pkgs, kinds=upgraded_kinds)

        log.info(
            event="upgrade_all_outdated_complete",
            upgraded=len(upgraded),
            failed=len(failures),
        )

        return upgraded, failures
