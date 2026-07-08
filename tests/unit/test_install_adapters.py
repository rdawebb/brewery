"""Unit tests for the orchestrator port adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewCommandError,
    PinnedPackageWarning,
)
from brewery.core.models import Package, PackageKind
from brewery.providers.install_adapters import BrewAdapter, RepositoryCatalogAdapter

pytestmark = pytest.mark.asyncio


class MockCatalog:
    """Minimal catalog stub that records calls and returns predictable values."""

    def __init__(self) -> None:
        """Initialise the mock catalog with an empty call log."""
        self.calls = []

    def get_formula(self, name: str) -> str:
        """Record the call and return a row sentinel string.

        Args:
            name: The formula name to look up.

        Returns:
            A sentinel string `"row:<name>"`.
        """
        self.calls.append(("get_formula", name))

        return f"row:{name}"

    def resolve_alias(self, name: str) -> str:
        """Record the call and return a canonical name sentinel string.

        Args:
            name: The alias to resolve.

        Returns:
            A sentinel string `"canon:<name>"`.
        """
        self.calls.append(("resolve_alias", name))

        return f"canon:{name}"

    def runtime_deps(self, name: str) -> list[str]:
        """Record the call and return a single predictable dependency.

        Args:
            name: The formula name.

        Returns:
            A list containing `"<name>-dep"`.
        """
        self.calls.append(("runtime_deps", name))

        return [f"{name}-dep"]

    def aliases_of(self, name: str) -> list[str]:
        """Record the call and return a single predictable alias.

        Args:
            name: The formula name.

        Returns:
            A list containing `"<name>-alias"`.
        """
        self.calls.append(("aliases_of", name))

        return [f"{name}-alias"]


class MockCacheMgr:
    """Minimal cache manager stub that reports a fixed set of installed packages."""

    def __init__(self, installed: dict[str, str | None] | None = None) -> None:
        """Initialise the mock cache manager.

        Args:
            installed: Mapping of package name -> keg path (or None for no path).
        """
        self._installed: dict[str, str | None] = installed or {}
        self.calls: list = []

    def find_installed(self, name: str, kind: PackageKind) -> Package | None:
        """Return a Package if *name* is in the installed set, else None.

        Args:
            name: The package name to look up.
            kind: The package kind.

        Returns:
            A Package with path if installed, else None.
        """
        self.calls.append((name, kind))
        if name not in self._installed:
            return None

        return Package(name, kind, path=self._installed[name])


class MockRepo:
    """Minimal repo stub wiring together a MockCatalog and MockCacheMgr."""

    def __init__(self, installed: dict[str, str | None] | None = None) -> None:
        """Initialise the mock repo.

        Args:
            installed: Mapping of package name -> keg path passed to MockCacheMgr.
        """
        self.catalog = MockCatalog()
        self.cache_mgr = MockCacheMgr(installed)
        self.formula = None


async def test_catalog_methods_delegate_to_repo_catalog() -> None:
    """Test that each CatalogPort method delegates to repo.catalog."""
    repo = MockRepo(installed={})
    adapter = RepositoryCatalogAdapter(repo)
    assert adapter.get_formula("wget") == "row:wget"
    assert adapter.resolve_alias("py") == "canon:py"
    assert adapter.runtime_deps("wget") == ["wget-dep"]
    assert adapter.aliases_of("openssl@3") == ["openssl@3-alias"]
    assert ("get_formula", "wget") in repo.catalog.calls
    assert ("aliases_of", "openssl@3") in repo.catalog.calls


async def test_is_satisfied_true_when_installed_with_receipt(
    tmp_path: Path,
) -> None:
    """Test that is_satisfied returns True when the keg has a valid receipt."""
    keg = tmp_path / "wget" / "1.21.4"
    keg.mkdir(parents=True)
    (keg / "INSTALL_RECEIPT.json").write_text(json.dumps({}))

    repo = MockRepo(installed={"wget": str(keg)})
    adapter = RepositoryCatalogAdapter(repo)
    assert adapter.is_satisfied("wget") is True
    assert repo.cache_mgr.calls == [("wget", PackageKind.FORMULA)]


async def test_is_satisfied_false_when_absent() -> None:
    """Test that is_satisfied returns False when the package is absent from the cache."""
    repo = MockRepo(installed={})
    adapter = RepositoryCatalogAdapter(repo)
    assert adapter.is_satisfied("wget") is False


async def test_is_satisfied_false_when_keg_has_no_receipt(tmp_path: Path) -> None:
    """Test that is_satisfied returns False for a keg with no INSTALL_RECEIPT.json.

    This is the interrupted-install case: the keg directory exists in the Cellar
    but the install pipeline never completed, so there is no receipt.
    """
    keg = tmp_path / "wget" / "1.21.4"
    keg.mkdir(parents=True)

    repo = MockRepo(installed={"wget": str(keg)})
    adapter = RepositoryCatalogAdapter(repo)
    assert adapter.is_satisfied("wget") is False


async def test_is_satisfied_false_when_pkg_has_no_path() -> None:
    """Test that is_satisfied returns False when find_installed returns a Package with no path."""
    repo = MockRepo(installed={"wget": None})
    adapter = RepositoryCatalogAdapter(repo)
    assert adapter.is_satisfied("wget") is False


class MockBackend:
    """Minimal formula backend stub with configurable install failure."""

    def __init__(self, exc=None) -> None:
        """Initialise the mock backend.

        Args:
            exc: An exception to raise from install(), or None to succeed.
        """
        self.exc = exc
        self.calls = []

    async def install(self, names) -> list[str]:
        """Record the call and optionally raise the configured exception.

        Args:
            names: The list of package names to install.

        Returns:
            The names list on success.
        """
        self.calls.append(names)
        if self.exc is not None:
            raise self.exc

        return names


class MockRunBrew:
    """Minimal brew runner stub that fails on a configured set of subcommands."""

    def __init__(self, fail_on=()) -> None:
        """Initialise the mock runner.

        Args:
            fail_on: Subcommand names (e.g. `"link"`) that should raise BrewCommandError.
        """
        self.fail_on = set(fail_on)  # Subcommands that should raise
        self.calls = []

    async def __call__(self, args) -> None:
        """Record the call and raise BrewCommandError if the subcommand is in fail_on.

        Args:
            args: The argument list passed to the runner.

        Raises:
            BrewCommandError: If the subcommand is in fail_on.
        """
        self.calls.append(args)
        if args and args[0] in self.fail_on:
            raise BrewCommandError(command="brew " + " ".join(args), returncode=1)

        return None


async def test_install_success_returns_true() -> None:
    """Test that a successful backend install returns True."""
    backend = MockBackend()
    adapter = BrewAdapter(backend, MockRunBrew())
    assert await adapter.install("wget") is True
    assert backend.calls == [["wget"]]


async def test_install_already_installed_is_success() -> None:
    """Test that AlreadyInstalledWarning from the backend is treated as success."""
    backend = MockBackend(exc=AlreadyInstalledWarning(package="wget"))
    adapter = BrewAdapter(backend, MockRunBrew())
    assert await adapter.install("wget") is True


async def test_install_command_error_is_failure() -> None:
    """Test that a BrewCommandError from the backend returns False."""
    backend = MockBackend(
        exc=BrewCommandError(command="brew install wget", returncode=1)
    )
    adapter = BrewAdapter(backend, MockRunBrew())
    assert await adapter.install("wget") is False


async def test_link_success_and_failure() -> None:
    """Test that link returns True on success and False when the runner raises."""
    run = MockRunBrew()
    adapter = BrewAdapter(MockBackend(), run)
    assert await adapter.link("wget") is True
    assert run.calls[-1] == ["link", "wget"]

    run_fail = MockRunBrew(fail_on={"link"})
    adapter = BrewAdapter(MockBackend(), run_fail)
    assert await adapter.link("wget") is False


async def test_post_install_success_and_failure() -> None:
    """Test that post_install returns True on success and False when the runner raises."""
    run = MockRunBrew()
    adapter = BrewAdapter(MockBackend(), run)
    assert await adapter.post_install("openssl@3") is True
    assert run.calls[-1] == ["postinstall", "openssl@3"]

    run_fail = MockRunBrew(fail_on={"postinstall"})
    adapter = BrewAdapter(MockBackend(), run_fail)
    assert await adapter.post_install("openssl@3") is False


async def test_pinned_warning_from_backend_propagates() -> None:
    """Test that PinnedPackageWarning is not swallowed and propagates to the caller."""
    backend = MockBackend(exc=PinnedPackageWarning(package="wget"))
    adapter = BrewAdapter(backend, MockRunBrew())
    with pytest.raises(PinnedPackageWarning):
        await adapter.install("wget")
