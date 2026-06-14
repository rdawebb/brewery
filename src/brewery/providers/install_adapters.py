"""Concrete bindings of the orchestrator's ports to brewery internals."""

from __future__ import annotations

from brewery.core.errors import AlreadyInstalledWarning, BrewCommandError
from brewery.core.models import PackageKind
from brewery.providers.orchestrator import BrewPort, CatalogPort, FormulaRowP


class RepositoryCatalogAdapter:  # implements orchestrator.CatalogPort
    """Binds CatalogPort to the existing Repository.

    Catalog lookups delegate to repo.catalog; installed-state (is_satisfied)
    delegates to repo.cache_mgr, since the catalog has no view of what's
    installed.
    """

    def __init__(self, repo) -> None:
        """Initialise the adapter.

        Args:
            repo: The repository instance providing catalog and cache access.
        """
        self._repo = repo
        self._catalog = repo.catalog

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
        """Return True if any version of the formula is already installed.

        Args:
            name: The canonical formula name to check.

        Returns:
            True if a matching installed keg is found in the cache, False otherwise.
        """
        # Any version counts as satisfied for a fresh install, matching brew
        return (
            self._repo.cache_mgr.find_installed(name, PackageKind.FORMULA) is not None
        )


# Static assertion that the adapter satisfies the port
_catalog_port: type[CatalogPort] = RepositoryCatalogAdapter


class BrewAdapter:
    """Binds BrewPort to brew passthrough.

    install() reuses the formula backend (which raises BrewCommandError on
    failure); link/post_install are not on the backend, so they go through the
    brew runner directly. Each method returns True on success, False on a brew
    failure, so the orchestrator can record the outcome without exceptions
    crossing the port boundary.
    """

    def __init__(self, formula_backend, run_brew) -> None:
        """Initialise the adapter.

        Args:
            formula_backend: e.g. brew_formula.backend (has async install()).
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

    async def link(self, name: str) -> bool:
        """Link a formula's keg into the prefix via `brew link`.

        Args:
            name: The canonical formula name to link.

        Returns:
            True on success, False on a brew failure.
        """
        try:
            await self._run_brew("link", name)
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
