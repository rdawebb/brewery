"""Repository module for managing package data from catalog and FS cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

from brewery.core.cache import Cache, CacheManager
from brewery.core.catalog import Catalog
from brewery.core.config import BreweryENV
from brewery.core.decorators import log_operation
from brewery.core.errors import PackageNotFoundError
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.core.shell import run_brew
from brewery.providers import brew

if TYPE_CHECKING:
    from brewery.providers.orchestrator import ProgressPort

log: BreweryLogger = get_logger(name=__name__)


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
        self, kind_filter: PackageKind | None = None
    ) -> list[Package]:
        """Get all installed packages, optionally filtered by kind.

        Args:
            kind_filter: Optional filter for package kind (formula or cask).

        Returns:
            A list of installed Package instances.
        """
        return self.cache_mgr.installed_packages(kind=kind_filter)

    @log_operation(event_prefix="get_details", log_args=["name", "kind"])
    def get_details(self, name: str, kind: PackageKind | None = None) -> Package:
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
        self,
        names: list[str],
        kind: PackageKind = PackageKind.FORMULA,
        *,
        progress: ProgressPort | None = None,
    ) -> tuple[list[Package], list[tuple[str, str]]]:
        """Install a package or packages and return details.

        Args:
            names: Name of the package(s) to install.
            kind: Kind of the package(s) - formula (default or cask).
            progress: Optional progress sink for the native pipeline.

        Returns:
            Package(s) details on success.

        Raises:
            BrewCommandError: Propagated from provider.
        """
        if kind == PackageKind.CASK:
            await self.cask.install(names=names)

        else:
            from brewery.providers.pipeline import run_install

            await run_install(
                names,
                catalog=self.catalog,
                cache_mgr=self.cache_mgr,
                formula=self.formula,
                run_brew=run_brew,
                progress=progress,
            )

        self.cache_mgr.invalidate()
        installed_by_name: dict[str, Package] = {
            p.name: p for p in self.cache_mgr.installed_packages(kind=kind)
        }

        resolved: dict[str, str] = {n: self.catalog.resolve_alias(n) for n in names}

        installed: list[Package] = [
            installed_by_name[resolved[n]]
            for n in names
            if resolved[n] in installed_by_name
        ]

        failures: list[tuple[str, str]] = [
            (n, "install failed or not found")
            for n in names
            if resolved[n] not in installed_by_name
        ]

        return installed, failures
