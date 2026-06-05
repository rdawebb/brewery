"""Integration tests: the Repository over a real on-disk Cache.

Cache writes go to the temp dir set up in the top-level conftest. The provider
subprocess boundary is mocked via mock_brew; everything else is real.
"""

from __future__ import annotations

import pytest

from brewery.core.models import PackageKind, PackageStatus

pytestmark = pytest.mark.integration


class TestGetAllInstalled:
    """Tests for the get_all_installed method."""

    async def test_refresh_returns_all_kinds_sorted(self, repo) -> None:
        """Test that get_all_installed returns all kinds sorted by (kind.value, name)."""
        pkgs = await repo.get_all_installed()

        # Sorted by (kind.value, name): casks before formulae, then by name.
        assert [p.name for p in pkgs] == ["iina", "act", "yazi"]
        assert [p.kind for p in pkgs] == [
            PackageKind.CASK,
            PackageKind.FORMULA,
            PackageKind.FORMULA,
        ]

    async def test_second_call_is_served_from_cache(self, repo, monkeypatch):
        """Test that get_all_installed serves from FS cache on subsequent calls."""
        await repo.get_all_installed()

        import brewery.core.cache as cache_mod

        def _boom(env=None):
            raise AssertionError("scan_installed should not be called on a cache hit")

        monkeypatch.setattr(cache_mod, "scan_installed", _boom)

        pkgs = await repo.get_all_installed()
        assert {p.name for p in pkgs} == {"iina", "act", "yazi"}

    async def test_kind_filter_returns_only_formulae(self, repo):
        """Test that get_all_installed returns only formulae when kind_filter is set to FORMULA."""
        pkgs = await repo.get_all_installed(kind_filter=PackageKind.FORMULA)
        assert [p.name for p in pkgs] == ["act", "yazi"]


class TestLiveOutdatedFlip:
    """End-to-end outdated detection: act is installed at 0.2.88 but the catalog
    reports 0.2.89 as the latest.  Outdated status is derived from the catalog
    comparison on every call; live=True forces an FS re-scan."""

    async def test_act_outdated_against_catalog(self, repo):
        """Catalog 0.2.89 > installed 0.2.88 → act is OUTDATED immediately."""
        outdated = await repo.get_outdated(live=True)

        assert {p.name for p in outdated} == {"act"}
        act = outdated[0]
        assert PackageStatus.OUTDATED in act.status
        assert act.metadata["latest_version"] == "0.2.89"

    async def test_live_scan_result_cached_for_subsequent_reads(self, repo):
        """FS records written during live=True are reused by the next live=False call."""
        await repo.get_outdated(live=True)

        cached_outdated = await repo.get_outdated(live=False)
        assert {p.name for p in cached_outdated} == {"act"}

    async def test_non_outdated_packages_keep_clean_status(self, repo):
        """Non-outdated packages stay clean after a live reconciliation."""
        await repo.get_outdated(live=True)
        all_pkgs = await repo.get_all_installed()
        yazi = next(p for p in all_pkgs if p.name == "yazi")
        iina = next(p for p in all_pkgs if p.name == "iina")
        assert PackageStatus.OUTDATED not in yazi.status
        assert PackageStatus.OUTDATED not in iina.status


class TestGetDetails:
    async def test_details_from_cache_after_refresh(self, repo):
        """Test that get_details serves from cache after refresh."""
        await repo.get_all_installed()
        pkg = await repo.get_details("yazi", PackageKind.FORMULA)
        assert pkg.name == "yazi"
        assert pkg.metadata["latest_version"] == "26.5.6"
