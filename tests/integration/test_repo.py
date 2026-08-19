"""Integration tests for Repository orchestration over catalog + scanner + providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brewery.core.repo import Repository

import pytest
from _repo_helpers import _add_alias, _provider_calls

from brewery.core.models import PackageKind, PackageStatus

pytestmark = pytest.mark.integration


def _repo_with_providers(catalog, *, formula=None, cask=None) -> Repository:
    """Build a Repository with per-test provider backends.

    The default brew_formula/brew_cask backends are shared module singletons, so
    a test must never mutate repo.formula/repo.cask in place. Injecting fresh
    backends via the constructor keeps stateful mocks isolated to one test.

    Args:
        catalog: The catalog to use for the repository.
        formula: An optional formula backend to use.
        cask: An optional cask backend to use.

    Returns:
        A Repository instance with the specified backends.
    """
    from types import SimpleNamespace

    from brewery.core.repo import Repository
    from brewery.providers import brew

    async def _noop(names) -> list[str]:
        """Simulate a no-op operation.

        Args:
            names: The names to operate on.

        Returns:
            The names unchanged.
        """
        return names

    formula_backend = SimpleNamespace(
        install=_noop,
        uninstall=formula or _noop,
        upgrade=formula or _noop,
    )
    cask_backend = SimpleNamespace(
        install=_noop, uninstall=cask or _noop, upgrade=cask or _noop
    )

    return Repository(
        catalog=catalog,
        formula_backend=formula_backend if formula else brew.formula_backend,
        cask_backend=cask_backend if cask else brew.cask_backend,
    )


class TestGetAllInstalled:
    """Tests for the get_all_installed method."""

    async def test_refresh_returns_all_kinds_sorted(self, repo) -> None:
        """Test that get_all_installed returns all kinds sorted by (kind.value, name)."""
        pkgs = repo.get_all_installed()

        # Sorted by (kind.value, name) -> None: casks before formulae, then by name
        assert [p.name for p in pkgs] == ["iina", "act", "yazi"]
        assert [p.kind for p in pkgs] == [
            PackageKind.CASK,
            PackageKind.FORMULA,
            PackageKind.FORMULA,
        ]

    async def test_second_call_is_served_from_cache(self, repo, monkeypatch) -> None:
        """Test that get_all_installed serves from FS cache on subsequent calls."""
        repo.get_all_installed()

        import brewery.core.cache as cache_mod

        def _boom(env=None) -> None:
            """Raise AssertionError to simulate scan_installed not being called on a cache hit.

            Args:
                env: Environment variable (unused)

            Raises:
                AssertionError: Always raised to simulate scan_installed not being called on a cache hit
            """
            raise AssertionError("scan_installed should not be called on a cache hit")

        monkeypatch.setattr(cache_mod, "scan_installed", _boom)

        pkgs = repo.get_all_installed()
        assert {p.name for p in pkgs} == {"iina", "act", "yazi"}

    async def test_kind_filter_returns_only_formulae(self, repo) -> None:
        """Test that get_all_installed returns only formulae when kind_filter is set to FORMULA."""
        pkgs = repo.get_all_installed(kind_filter=PackageKind.FORMULA)
        assert [p.name for p in pkgs] == ["act", "yazi"]


class TestOutdatedDerivation:
    """Outdated detection: act is installed at 0.2.88 but the catalog reports
    0.2.89 as the latest. OUTDATED is derived from the catalog comparison in the
    merge, so get_outdated() is a pure read over cached records with no network.
    Reconciling against a fresh catalog is the caller's job: await
    refresh_catalog(...) then call get_outdated()."""

    async def test_act_outdated_against_catalog(self, repo) -> None:
        """Test that Catalog 0.2.89 > installed 0.2.88 → act is OUTDATED."""
        outdated = repo.get_outdated()

        assert {p.name for p in outdated} == {"act"}
        act = outdated[0]
        assert PackageStatus.OUTDATED in act.status
        assert act.metadata["latest_version"] == "0.2.89"

    async def test_outdated_result_stable_across_reads(self, repo) -> None:
        """Test that a second read reports the same outdated set from the cached records."""
        repo.get_outdated()

        cached_outdated = repo.get_outdated()
        assert {p.name for p in cached_outdated} == {"act"}

    async def test_non_outdated_packages_keep_clean_status(self, repo) -> None:
        """Test that non-outdated packages stay clean."""
        repo.get_outdated()
        all_pkgs = repo.get_all_installed()
        yazi = next(p for p in all_pkgs if p.name == "yazi")
        iina = next(p for p in all_pkgs if p.name == "iina")
        assert PackageStatus.OUTDATED not in yazi.status
        assert PackageStatus.OUTDATED not in iina.status

    def test_get_outdated_does_not_refresh(self, repo, monkeypatch) -> None:
        """Test that get_outdated never touches the network/refresh path.

        The refresh is the caller's responsibility, so a read alone must not
        invoke refresh_catalog, and patching it to raise proves the read is pure.
        """
        import brewery.daemon.catalog_refresh as refresh_mod

        def _boom(*a, **k) -> None:
            """Raise AssertionError to simulate refresh_catalog not being called.

            Args:
                *a: Positional arguments
                **k: Keyword arguments

            Raises:
                AssertionError: Always raised to simulate refresh_catalog not being called
            """
            raise AssertionError("get_outdated must not refresh")

        monkeypatch.setattr(refresh_mod, "refresh_catalog", _boom)
        outdated = repo.get_outdated()
        assert {p.name for p in outdated} == {"act"}

    async def test_caller_refresh_then_read(self, repo, monkeypatch) -> None:
        """Test the caller-side sequence: refresh first, then a pure read.

        This mirrors what the CLI's `outdated --check` does: await the refresh
        in the caller's async context, then call the sync get_outdated().
        """
        called = {"n": 0}

        async def mock_refresh(*, catalog) -> None:
            """Simulate a refresh by incrementing the call counter.

            Args:
                *a: Positional arguments
                **k: Keyword arguments
            """
            called["n"] += 1

        import brewery.daemon.catalog_refresh as refresh_mod

        monkeypatch.setattr(refresh_mod, "refresh_catalog", mock_refresh)

        await refresh_mod.refresh_catalog(catalog=repo.catalog)
        repo.cache_mgr.invalidate()
        outdated = repo.get_outdated()

        assert called["n"] == 1
        assert {p.name for p in outdated} == {"act"}


class TestGetDetails:
    """Test cases for Repository.get_details."""

    async def test_details_from_cache_after_refresh(self, repo) -> None:
        """Test that get_details serves from cache after refresh."""
        repo.get_all_installed()
        pkg = repo.get_details("yazi", PackageKind.FORMULA)
        assert pkg.name == "yazi"
        assert pkg.metadata["latest_version"] == "26.5.6"


class TestSearch:
    """Tests for Repository.search."""

    async def test_search_finds_catalog_formula(self, repo) -> None:
        """Test that a catalog formula is returned by a name search."""
        results = repo.search("yazi")
        assert any(p.name == "yazi" for p in results)

    async def test_installed_result_is_enriched(self, repo) -> None:
        """Test that an installed hit carries installed status, not catalog-only.

        act is installed (0.2.88) and outdated against the catalog (0.2.89), so a
        search hit for it should be the merged installed package flagged OUTDATED,
        not a bare catalog entry.
        """
        results = repo.search("act")
        act = next(p for p in results if p.name == "act")
        assert act.versions == ["0.2.88"]
        assert PackageStatus.OUTDATED in act.status

    async def test_no_match_returns_empty(self, repo) -> None:
        """Test that a non-matching term returns no results.

        Args:
            repo: The Repository instance to test with
        """
        assert repo.search("zzzznomatch") == []


class TestInstall:
    """Tests for Repository.install_packages."""

    async def test_install_calls_provider(self, repo, mock_brew) -> None:
        """Test that installing a formula not already in the Cellar falls back to brew install."""
        await repo.install_packages(["ripgrep"], kind=PackageKind.FORMULA)
        assert _provider_calls(mock_brew, "install")

    async def test_install_reports_present_package_as_installed(self, repo) -> None:
        """Test that a package present on the mock fs is reported installed.

        yazi already exists in the mock Cellar, so after the (mocked) install and
        re-scan it is found and returned.
        """
        installed, failures = await repo.install_packages(
            ["yazi"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["yazi"]
        assert failures == []

    async def test_install_reports_absent_package_as_failure(self, repo) -> None:
        """Test that a package absent from the fs after install is a failure.

        The mock does not create the keg, so a never-installed name re-scans as
        missing and is reported as a failure rather than a success.
        """
        installed, failures = await repo.install_packages(
            ["ripgrep"], kind=PackageKind.FORMULA
        )
        assert installed == []
        assert failures == [("ripgrep", "install failed or not found")]

    async def test_install_appearing_package_is_detected(self, repo, mock_env) -> None:
        """Test that a keg created during install is detected on re-scan.

        Simulating brew creating the keg (plus a receipt) makes the package show
        up after invalidation, exercising the cache-invalidate-then-rescan path.
        """
        import orjson

        keg = mock_env.cellar / "ripgrep" / "14.1.0"
        keg.mkdir(parents=True)
        (keg / "INSTALL_RECEIPT.json").write_bytes(
            orjson.dumps({"source": {"tap": "homebrew/core"}})
        )
        installed, failures = await repo.install_packages(
            ["ripgrep"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["ripgrep"]
        assert failures == []

    async def test_install_via_alias_verified_by_canonical_name(self, repo) -> None:
        """Test that an installed alias verifies against its canonical name.

        Requesting "yazi-cli" (an alias for the present "yazi") must report the
        canonical package as installed.
        """
        _add_alias(repo.catalog, "yazi-cli", "yazi")
        installed, failures = await repo.install_packages(
            ["yazi-cli"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["yazi"]
        assert failures == []
