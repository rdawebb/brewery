"""Repository module for managing package data from various backends."""

from __future__ import annotations

import asyncio
from typing import Optional

from brewery.core.cache import Cache, CacheManager
from brewery.core.config import get_brewery_env
from brewery.core.decorators import log_operation
from brewery.core.errors import PackageNotFoundError
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.providers import brew_cask, brew_formula, brew_outdated


class Repository:
    """Repository for managing package data from various backends."""

    def __init__(
        self, cache: Cache | None = None, cache_mgr: CacheManager | None = None
    ):
        """Initialise the repository."""
        _cache = cache or Cache(namespace="repository")
        self.cache_mgr = cache_mgr or CacheManager(_cache)

    @log_operation(event_prefix="get_all_installed", log_args=["kind_filter"])
    async def get_all_installed(
        self, kind_filter: Optional[PackageKind] = None
    ) -> list[Package]:
        """Get all installed packages, optionally filtered by kind.

        Args:
            kind_filter: Optional filter for package kind (formula or cask).

        Returns:
            A list of installed Package instances.
        """
        pkgs: list[Package] = await self.cache_mgr.load_packages(kind=kind_filter)
        if not pkgs:
            pkgs: list[Package] = await self.cache_mgr.refresh_packages(
                kind=kind_filter
            )

        return pkgs

    @log_operation(event_prefix="get_details", log_args=["name", "kind"])
    async def get_details(
        self, name: str, kind: Optional[PackageKind] = None
    ) -> Package:
        """Get package details by name and kind.

        Args:
            name: Name of the package.
            kind: Kind of the package (formula or cask).

        Returns:
            A Package instance with detailed information.
        """
        if kind is None:
            cache_results = await asyncio.gather(
                self.cache_mgr.get_details_from_cache(name, kind=PackageKind.FORMULA),
                self.cache_mgr.get_details_from_cache(name, kind=PackageKind.CASK),
            )
            for pkg in cache_results:
                if pkg:
                    return pkg

            results = await asyncio.gather(
                brew_formula.info(names=[name]),
                brew_cask.info(names=[name]),
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, list) and result:
                    return result[0]

            raise PackageNotFoundError(package=name)

        else:
            pkg = await self.cache_mgr.get_details_from_cache(name, kind=kind)
            if pkg:
                return pkg

            raise PackageNotFoundError(package=name)

    @log_operation(event_prefix="install_package", log_args=["name", "kind"])
    async def install_packages(
        self, names: list[str], kind: PackageKind = PackageKind.FORMULA
    ) -> tuple[list[Package], list[tuple[str, str]]]:
        """Install a package or packages and return details.

        Args:
            names: Name of the package(s) to install.
            kind: Kind of the package(s) - formula (default or cask).

        Returns:
            Package(s) details on success.

        Raises:
            BrewCommandError: Propagated from provider.
        """
        provider = brew_formula if kind == PackageKind.FORMULA else brew_cask

        await provider.install(names=names)

        installed_pkgs: list[Package] = await provider.info(names=names)
        installed_names: list[str] = [p.name for p in installed_pkgs if p.versions]

        failures: list[tuple[str, str]] = [
            (name, "install failed or not found")
            for name in names
            if name not in installed_names
        ]

        await self.cache_mgr.update_packages(packages=installed_pkgs, action="add")

        return installed_pkgs, failures

    @log_operation(event_prefix="uninstall_package", log_args=["name", "kind"])
    async def uninstall_packages(
        self, names: list[str], kind: PackageKind | None = None
    ) -> tuple[int, list[tuple[str, str]]]:
        """Uninstall packages and refresh cache on success.

        Args:
            names: Name(s) of the package(s) to uninstall.
            kind: Kind of the package(s) (formula or cask).

        Returns:
            Number of successes, and list of failures

        Raises:
            BrewCommandError: Propagated from provider.
        """
        if kind is None:
            # Resolve kinds and split into two lists
            all_pkgs: list[Package] = await self.get_all_installed()
            kind_map: dict[str, PackageKind] = {p.name: p.kind for p in all_pkgs}
            formula_names: list[str] = [
                n for n in names if kind_map.get(n) == PackageKind.FORMULA
            ]
            cask_names: list[str] = [
                n for n in names if kind_map.get(n) == PackageKind.CASK
            ]
            failures: list[tuple[str, str]] = [
                (n, "not found") for n in names if n not in kind_map
            ]
        else:
            formula_names: list[str] | list = (
                names if kind == PackageKind.FORMULA else []
            )
            cask_names: list[str] | list = names if kind == PackageKind.CASK else []
            failures: list = []

        succeeded = 0

        for pkg_names, provider, pkg_kind in [
            (formula_names, brew_formula, PackageKind.FORMULA),
            (cask_names, brew_cask, PackageKind.CASK),
        ]:
            if not pkg_names:
                continue

            await provider.uninstall(names=pkg_names)

            removed, failed_names = await self._verify_removed(pkg_names, pkg_kind)
            failures.extend((n, "uninstall failed") for n in failed_names)

            succeeded += len(removed)

            await self.cache_mgr.update_packages(
                packages=[Package(name=n, kind=pkg_kind) for n in removed],
                action="remove",
            )

        return succeeded, failures

    async def _verify_removed(
        self, names: list[str], kind: PackageKind
    ) -> tuple[list[str], list[str]]:
        """Return (removed, failed) based on filesystem presence.

        Args:
            names: List of package names to verify.
            kind: Package kind (formula or cask).

        Returns:
            Tuple of (removed, failed) package names.
        """
        env = get_brewery_env()

        base = env.cellar if kind == PackageKind.FORMULA else env.caskroom

        removed, failed = [], []
        for name in names:
            (failed if (base / name).exists() else removed).append(name)

        return removed, failed

    @log_operation(event_prefix="get_outdated", log_args=["name", "kind"])
    async def get_outdated(self, live: bool = False) -> list[Package]:
        """Return a list of outdated packages.

        Args:
            live: If True, call brew directly and refresh cache, otherwise use cached data.

        Returns:
            List of packages with OUTDATED status.
        """
        if live:
            outdated_entries = await brew_outdated.fetch_outdated()
            await self.cache_mgr.refresh_outdated_status(outdated_entries)

            all_pkgs = await self.get_all_installed()
            outdated_pkgs = [p for p in all_pkgs if PackageStatus.OUTDATED in p.status]

            return outdated_pkgs

        else:
            # Use cached data
            all_pkgs: list[Package] = await self.get_all_installed()
            return [p for p in all_pkgs if PackageStatus.OUTDATED in p.status]

    @log_operation(event_prefix="upgrade_packages", log_args=["names", "kind"])
    async def upgrade_packages(
        self, names: list[str] | None = None, kind: PackageKind | None = None
    ) -> tuple[list[Package], list[Package], list[tuple[str, str]]]:
        """Upgrade packages and refresh cache entry.

        Args:
            names: Name(s) of the package(s) to upgrade.
            kind: Kind of the package(s) (formula, cask, auto (default))

        Returns:
            Details of upgraded packages, already up-to-date packages, and any failures.

        Raises:
            BrewCommandError: Propagated from provider.
            PackagePinnedWarning: If any packages are pinned.
        """
        # Upgrade all
        if names is None:
            outdated: list[Package] = await self.get_outdated(live=False)
            pinned: list[tuple[str, str]] = [
                (p.name, "pinned - skipped")
                for p in outdated
                if PackageStatus.PINNED in p.status
            ]
            to_upgrade: list[Package] = [
                p for p in outdated if PackageStatus.PINNED not in p.status
            ]
            formula_names: list[str] = [
                p.name for p in to_upgrade if p.kind == PackageKind.FORMULA
            ]
            cask_names: list[str] = [
                p.name for p in to_upgrade if p.kind == PackageKind.CASK
            ]
            failures: list[tuple[str, str]] = pinned

        # Upgrade specified
        else:
            if kind is None:
                all_pkgs: list[Package] = await self.get_all_installed()
                kind_map: dict[str, PackageKind] = {p.name: p.kind for p in all_pkgs}
                formula_names: list[str] = [
                    n for n in names if kind_map.get(n) == PackageKind.FORMULA
                ]
                cask_names: list[str] = [
                    n for n in names if kind_map.get(n) == PackageKind.CASK
                ]
                failures: list[tuple[str, str]] = [
                    (n, "not found") for n in names if n not in kind_map
                ]
            else:
                formula_names: list = names if kind == PackageKind.FORMULA else []
                cask_names: list = names if kind == PackageKind.CASK else []
                failures: list = []

        upgraded_pkgs: list[Package] = []
        current_pkgs: list[Package] = []

        for pkg_names, provider in [
            (formula_names, brew_formula),
            (cask_names, brew_cask),
        ]:
            if not pkg_names:
                continue

            pre_fetch: list[Package] = await provider.info(names=pkg_names)
            current_versions: dict[str, str | None] = {
                p.name: p.versions[0] if p.versions else None for p in pre_fetch
            }

            await provider.upgrade(names=pkg_names)
            pkgs: list[Package] = await provider.info(names=pkg_names)

            for pkg in pkgs:
                if pkg.versions and pkg.versions[0] != current_versions.get(pkg.name):
                    upgraded_pkgs.append(pkg)
                else:
                    current_pkgs.append(pkg)

        if upgraded_pkgs:
            await self.cache_mgr.update_packages(
                packages=upgraded_pkgs, action="update"
            )

        return upgraded_pkgs, current_pkgs, failures
