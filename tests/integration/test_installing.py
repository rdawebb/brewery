"""Integration tests for the install service over a real prefix."""

from __future__ import annotations

import pytest
from _repo_helpers import _add_alias, _provider_calls

from brewery.core.models import PackageKind
from brewery.services.install import install_packages

pytestmark = pytest.mark.integration


class TestInstall:
    """Tests for the install service."""

    async def test_install_calls_provider(self, repo, mock_brew) -> None:
        """Test that installing a formula not already in the Cellar falls back to brew install."""
        await install_packages(repo, ["ripgrep"], kind=PackageKind.FORMULA)
        assert _provider_calls(mock_brew, "install")

    async def test_install_reports_present_package_as_installed(self, repo) -> None:
        """Test that a package present on the mock fs is reported installed.

        yazi already exists in the mock Cellar, so after the (mocked) install and
        re-scan it is found and returned.
        """
        installed, failures = await install_packages(
            repo, ["yazi"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["yazi"]
        assert failures == []

    async def test_install_reports_absent_package_as_failure(self, repo) -> None:
        """Test that a package absent from the fs after install is a failure.

        The mock does not create the keg, so a never-installed name re-scans as
        missing and is reported as a failure rather than a success.
        """
        installed, failures = await install_packages(
            repo, ["ripgrep"], kind=PackageKind.FORMULA
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
        installed, failures = await install_packages(
            repo, ["ripgrep"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["ripgrep"]
        assert failures == []

    async def test_install_via_alias_verified_by_canonical_name(self, repo) -> None:
        """Test that an installed alias verifies against its canonical name.

        Requesting "yazi-cli" (an alias for the present "yazi") must report the
        canonical package as installed.
        """
        _add_alias(repo.catalog, "yazi-cli", "yazi")
        installed, failures = await install_packages(
            repo, ["yazi-cli"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["yazi"]
        assert failures == []
