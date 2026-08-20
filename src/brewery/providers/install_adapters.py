"""Concrete bindings of the orchestrator's ports to brewery internals."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from brewery.core.errors import AlreadyInstalledWarning, BrewCommandError
from brewery.core.models import Package, PackageKind
from brewery.providers.base import InstallBackend, UpgradeBackend
from brewery.providers.orchestrator import BrewPort, CatalogPort, FormulaRowP


class CatalogSource(Protocol):
    """The catalog reads the adapter forwards (satisfied by `core.catalog.Catalog`).

    Narrower than `Catalog` on purpose: naming only these four keeps the adapter
    checkable without dragging the whole catalog surface across the boundary.
    """

    def get_formula(self, name: str) -> FormulaRowP | None:
        """Get a formula row by canonical name."""
        ...

    def resolve_alias(self, name: str) -> str:
        """Resolve an alias to its canonical formula name."""
        ...

    def runtime_deps(self, name: str) -> list[str]:
        """Direct runtime dependency names for a formula."""
        ...

    def aliases_of(self, name: str) -> list[str]:
        """Aliases that resolve to a formula."""
        ...


class InstalledSource(Protocol):
    """The installed-state read the adapter needs (satisfied by `CacheManager`)."""

    def find_installed(
        self, name: str, kind: PackageKind | None = None
    ) -> Package | None:
        """Return one installed package by name, or None."""
        ...


class FallbackBackend(InstallBackend, UpgradeBackend, Protocol):
    """The two verbs `BrewAdapter` delegates to the formula backend."""


class CatalogAdapter:  # Implements orchestrator.CatalogPort
    """Binds CatalogPort to the catalog and the installed-state cache.

    Catalog lookups delegate to the catalog; installed-state (is_satisfied)
    delegates to the cache manager, since the catalog has no view of what's
    installed.

    Every method here must be called on the thread that opened the catalog, i.e.
    the event loop; resolve what a worker needs before handing off to
    `asyncio.to_thread`, never from inside one. See `Catalog`'s docstring.
    """

    def __init__(self, catalog: CatalogSource, cache_mgr: InstalledSource) -> None:
        """Initialise the adapter.

        Args:
            catalog: The catalog backing formula/alias/dependency lookups.
            cache_mgr: The installed-state cache, for `is_satisfied`.
        """
        self._catalog = catalog
        self._cache_mgr = cache_mgr

    def get_formula(self, name: str) -> FormulaRowP | None:
        """Get a formula by name.

        Args:
            name: The canonical formula name to look up.

        Returns:
            The formula row if found, else None.
        """
        return self._catalog.get_formula(name)

    def resolve_alias(self, name: str) -> str:
        """Resolve a formula alias to its canonical name.

        Args:
            name: The name or alias to resolve.

        Returns:
            The canonical formula name.
        """
        return self._catalog.resolve_alias(name)

    def runtime_deps(self, name: str) -> list[str]:
        """Direct runtime dependency names for a formula.

        Args:
            name: The canonical formula name.

        Returns:
            A list of canonical names of its direct runtime dependencies.
        """
        return self._catalog.runtime_deps(name)

    def aliases_of(self, name: str) -> list[str]:
        """Aliases that resolve to this formula (reverse of the alias table).

        Args:
            name: The canonical formula name.

        Returns:
            A list of alias strings that map to this formula.
        """
        return self._catalog.aliases_of(name)

    def is_satisfied(self, name: str) -> bool:
        """Return True if a complete keg for the formula is already installed.

        A keg without an INSTALL_RECEIPT.json is treated as incomplete (e.g. an
        interrupted install) and will be reinstalled.

        Args:
            name: The canonical formula name to check.

        Returns:
            True if a complete installed keg is found in the cache, False otherwise.
        """
        pkg = self._cache_mgr.find_installed(name, PackageKind.FORMULA)
        if pkg is None or not pkg.path:
            return False

        return (Path(pkg.path) / "INSTALL_RECEIPT.json").exists()


# Static assertion that the adapter satisfies the port
_catalog_port: type[CatalogPort] = CatalogAdapter


class BrewAdapter:
    """Binds BrewPort to brew passthrough.

    install() reuses the formula backend (which raises BrewCommandError on
    failure); link/post_install are not on the backend, so they go through the
    brew runner directly. Each method returns True on success, False on a brew
    failure, so the orchestrator can record the outcome without exceptions
    crossing the port boundary.
    """

    def __init__(self, formula_backend: FallbackBackend, run_brew) -> None:
        """Initialise the adapter.

        Args:
            formula_backend: e.g. brew.formula_backend (has async install()).
            run_brew: async callable invoking `brew <args>` (your passthrough
                runner), raising BrewCommandError on a non-zero exit.
        """
        self._backend = formula_backend
        self._run_brew = run_brew

    async def install(self, name: str) -> bool:
        """Install a formula via the formula backend.

        Args:
            name: The canonical formula name to install.

        Returns:
            True on success or if already installed, False on a brew failure.
        """
        try:
            await self._backend.install(names=[name])
            return True

        except AlreadyInstalledWarning:
            return True

        except BrewCommandError:
            return False

    async def upgrade(self, name: str) -> bool:
        """Upgrade a formula via the formula backend.

        Args:
            name: The canonical formula name to upgrade.

        Returns:
            True on success, False on a brew failure.
        """
        try:
            await self._backend.upgrade(names=[name])
            return True

        except BrewCommandError:
            return False

    async def link(self, name: str) -> bool:
        """Link a formula's keg into the prefix via `brew link`.

        Args:
            name: The canonical formula name to link.

        Returns:
            True on success, False on a brew failure.
        """
        try:
            await self._run_brew(["link", name])
            return True

        except BrewCommandError:
            return False

    async def post_install(self, name: str) -> bool:
        """Run post-install hooks for a formula via `brew postinstall`.

        Args:
            name: The canonical formula name to run post-install steps for.

        Returns:
            True on success, False on a brew failure.
        """
        try:
            await self._run_brew(["postinstall", name])
            return True

        except BrewCommandError:
            return False


_brew_port: type[BrewPort] = BrewAdapter
