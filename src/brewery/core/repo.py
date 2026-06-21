"""Repository module for managing package data from catalog and FS cache."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from brewery.core.cache import Cache, CacheManager
from brewery.core.catalog import Catalog
from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.decorators import log_operation
from brewery.core.errors import PackageNotFoundError
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.core.shell import run_brew
from brewery.providers import brew


class Repository:
    """Repository for managing package data from various backends."""

    def __init__(
        self,
        cache: Cache | None = None,
        catalog: Catalog | None = None,
        cache_mgr: CacheManager | None = None,
        formula_backend=brew.formula_backend,
        cask_backend=brew.cask_backend,
        env: BreweryENV | None = None,
    ) -> None:
        """Initialise the repository.

        Args:
            cache: Optional cache instance.
            catalog: Optional catalog instance.
            cache_mgr: Optional cache manager instance.
            formula_backend: Backend for formulae.
            cask_backend: Backend for casks.
            env: Optional Brewery environment.
        """
        _cache = cache or Cache(namespace="repository")
        self.catalog: Catalog = catalog or Catalog()
        self.cache_mgr: CacheManager = cache_mgr or CacheManager(
            _cache, self.catalog, env
        )
        self.formula = formula_backend
        self.cask = cask_backend

    def close(self) -> None:
        """Close the catalog connection."""
        self.catalog.close()

    @log_operation(event_prefix="get_all_installed", log_args=["kind_filter"])
    def get_all_installed(
        self, kind_filter: Optional[PackageKind] = None
    ) -> list[Package]:
        """Get all installed packages, optionally filtered by kind.

        Args:
            kind_filter: Optional filter for package kind (formula or cask).

        Returns:
            A list of installed Package instances.
        """
        return self.cache_mgr.installed_packages(kind=kind_filter)

    @log_operation(event_prefix="get_details", log_args=["name", "kind"])
    def get_details(self, name: str, kind: Optional[PackageKind] = None) -> Package:
        """Get package details by name and kind.

        Args:
            name: Package name, alias, or cask token.
            kind: Optional kind filter (formula or cask)

        Returns:
            A Package instance with detailed information.

        Raises:
            PackageNotFoundError: If the package is not found.
        """
        match: Package | None = self.cache_mgr.find_installed(name, kind)
        if match is not None:
            return match

        from brewery.core.merge import catalog_info

        catalog_pkg: Package | None = catalog_info(catalog=self.catalog, name=name)
        if catalog_pkg is not None and (kind is None or catalog_pkg.kind == kind):
            return catalog_pkg

        raise PackageNotFoundError(package=name)

    @log_operation(event_prefix="search", log_args=["term"])
    def search(self, term: str) -> list[Package]:
        """Search the whole catalog, enriching results that are installed.

        Args:
            term: Search term to match against package names and descriptions.

        Returns:
            A list of Package instances matching the search term.
        """
        from brewery.core.merge import search_packages

        installed: dict[str, Package] = {
            p.name: p for p in self.cache_mgr.installed_packages()
        }

        return search_packages(catalog=self.catalog, query=term, installed=installed)

    @log_operation(event_prefix="get_outdated")
    def get_outdated(self) -> list[Package]:
        """Return outdated packages (OUTDATED is derived in the merge).

        Returns:
            Packages flagged OUTDATED.
        """
        packages: list[Package] = self.cache_mgr.installed_packages()

        return [p for p in packages if PackageStatus.OUTDATED in p.status]

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
        if kind == PackageKind.CASK:
            await self.cask.install(names=names)

        else:
            from brewery.providers.install_service import run_install

            await run_install(self, names, run_brew=run_brew)

        self.cache_mgr.invalidate()
        installed_by_name: dict[str, Package] = {
            p.name: p for p in self.cache_mgr.installed_packages(kind=kind)
        }

        installed: list[Package] = [
            installed_by_name[n] for n in names if n in installed_by_name
        ]

        failures: list[tuple[str, str]] = [
            (n, "install failed or not found")
            for n in names
            if n not in installed_by_name
        ]

        return installed, failures

    @log_operation(event_prefix="uninstall_package", log_args=["name", "kind"])
    async def uninstall_packages(
        self, names: list[str], kind: PackageKind | None = None
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Uninstall packages and refresh cache on success.

        Args:
            names: Name(s) of the package(s) to uninstall.
            kind: Kind of the package(s) (formula or cask).

        Returns:
            List of successfully removed package names, and list of (name, reason) failures

        Raises:
            BrewCommandError: Propagated from provider.
        """
        if kind is None:
            # Resolve kinds and split into two lists
            all_pkgs: list[Package] = self.get_all_installed()
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
            formula_names: list[str] = names if kind == PackageKind.FORMULA else []
            cask_names: list[str] = names if kind == PackageKind.CASK else []
            failures: list = []

        blocked = self._blocking_dependents(set(formula_names))
        if blocked:
            failures.extend(
                (name, f"required by {', '.join(deps)}")
                for name, deps in blocked.items()
            )
            formula_names = [n for n in formula_names if n not in blocked]

        if formula_names:
            from brewery.providers.uninstall_service import run_uninstall

            await run_uninstall(self, formula_names)

        if cask_names:
            await self.cask.uninstall(names=cask_names)

        self.cache_mgr.invalidate()

        removed: list[str] = []
        failed: list[str] = []

        for pkg_names, k in [
            (formula_names, PackageKind.FORMULA),
            (cask_names, PackageKind.CASK),
        ]:
            if not pkg_names:
                continue

            r, f = self._verify_removed(pkg_names, k)
            removed += r
            failed += f

        failures.extend((n, "uninstall failed") for n in failed)

        return removed, failures

    def _blocking_dependents(self, removal: set[str]) -> dict[str, list[str]]:
        """Installed formulae outside `removal` that still require a target.

        Reads each target's receipt-derived reverse-deps and drops any dependent
        that is itself being removed in the same batch.

        Args:
            removal: Canonical formula names slated for removal.

        Returns:
            target -> sorted installed formulae that require it (empty if none).
        """
        if not removal:
            return {}

        installed = {
            p.name: p
            for p in self.cache_mgr.installed_packages(kind=PackageKind.FORMULA)
        }

        blockers: dict[str, list[str]] = {}
        for name in removal:
            pkg = installed.get(name)
            if pkg is None:
                continue

            deps = sorted(d for d in pkg.used_by if d not in removal)
            if deps:
                blockers[name] = deps

        return blockers

    def _verify_removed(
        self, names: list[str], kind: PackageKind
    ) -> tuple[list[str], list[str]]:
        """Return (removed, failed) based on filesystem presence.

        Args:
            names: List of package names to verify.
            kind: Package kind (formula or cask).

        Returns:
            Tuple of (removed, failed) package names.
        """
        env = self.cache_mgr.env or get_brewery_env()

        base_dir = env.cellar if kind == PackageKind.FORMULA else env.caskroom

        removed, failed = [], []
        for name in names:
            (failed if (base_dir / name).exists() else removed).append(name)

        return removed, failed

    @log_operation(event_prefix="upgrade_packages", log_args=["names", "kind"])
    async def upgrade_packages(
        self, names: list[str] | None = None, kind: PackageKind | None = None
    ) -> tuple[list[Package], list[Package], list[tuple[str, str]]]:
        """Upgrade packages and report upgraded, already up-to-date, and failures.

        Args:
            names: Name(s) of the package(s) to upgrade.
            kind: Kind of the package(s) (formula, cask, auto (default))

        Returns:
            Details of upgraded packages, already up-to-date packages, and any failures.

        Raises:
            BrewCommandError: Propagated from provider.
            PackagePinnedWarning: If any packages are pinned.
        """
        installed: list[Package] = self.cache_mgr.installed_packages()
        by_name: dict[str, Package] = {p.name: p for p in installed}

        # Resolve the target set and any pinned skips
        if names is None:
            targets = [p for p in installed if PackageStatus.OUTDATED in p.status]
            failures = [
                (p.name, "pinned - skipped")
                for p in targets
                if PackageStatus.PINNED in p.status
            ]
            targets = [p for p in targets if PackageStatus.PINNED not in p.status]

        # Upgrade specified
        else:
            targets = [by_name[n] for n in names if n in by_name]
            failures = [(n, "not found") for n in names if n not in by_name]

        if kind is not None:
            targets = [p for p in targets if p.kind == kind]

        formula_names = [p.name for p in targets if p.kind == PackageKind.FORMULA]
        cask_names = [p.name for p in targets if p.kind == PackageKind.CASK]
        pre_versions: dict[str, str | None] = {
            p.name: (p.versions[0] if p.versions else None) for p in targets
        }

        if formula_names:
            from brewery.providers.upgrade_service import run_upgrade

            old_kegs = {
                p.name: Path(p.path)
                for p in targets
                if p.kind == PackageKind.FORMULA and p.path
            }
            await run_upgrade(self, formula_names, old_kegs, run_brew=run_brew)

        if cask_names:
            await self.cask.upgrade(names=cask_names)

        self.cache_mgr.invalidate()

        post: dict[str, Package] = {
            p.name: p for p in self.cache_mgr.installed_packages()
        }

        upgraded: list[Package] = []
        current: list[Package] = []
        for name in formula_names + cask_names:
            pkg = post.get(name)
            if pkg is None:
                continue

            new_version = pkg.versions[0] if pkg.versions else None
            if new_version != pre_versions.get(name):
                upgraded.append(pkg)
            else:
                current.append(pkg)

        return upgraded, current, failures

    @log_operation(event_prefix="cleanup")
    async def cleanup_packages(
        self, max_age_days: int = 30
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Remove stale kegs replaced more than max_age_days ago.

        Args:
            max_age_days: Age threshold in days, defaults to 30.

        Returns:
            Tuple of (removed "name version" strings, (label, reason) failures).
        """
        import asyncio

        from brewery.providers.cellar import rmtree
        from brewery.providers.retention import cleanup_candidates

        env = self.cache_mgr.env or get_brewery_env()
        installed = self.cache_mgr.installed_packages(kind=PackageKind.FORMULA)
        active = {Path(p.path) for p in installed if p.path}

        removed: list[str] = []
        failures: list[tuple[str, str]] = []
        for c in cleanup_candidates(
            env.cellar, active=active, max_age_days=max_age_days
        ):
            label = f"{c.name} {c.version}"
            try:
                await asyncio.to_thread(rmtree, c.keg)
                removed.append(label)

            except OSError as e:
                failures.append((label, str(e)))

        if removed:
            self.cache_mgr.invalidate()

        return removed, failures
