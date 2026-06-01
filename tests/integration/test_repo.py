"""Integration tests: the Repository over a real on-disk Cache.

Cache writes go to the temp dir set up in the top-level conftest. The provider
subprocess boundary is mocked via mock_brew; everything else is real.
"""

from __future__ import annotations

import pytest

from brewery.core.models import PackageKind, PackageStatus
from brewery.core.repo import Repository

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(mock_brew) -> Repository:
    """Fixture for the Repository, using the mock_brew subprocess boundary."""
    # mock_brew (and its fake_env) must be active before the repo touches brew.
    return Repository()


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
        """Test that get_all_installed serves from cache on subsequent calls."""
        await repo.get_all_installed()

        async def _boom():
            raise AssertionError("provider should not be called on a cache hit")

        monkeypatch.setattr(repo.formula, "list_installed", _boom)
        monkeypatch.setattr(repo.cask, "list_installed", _boom)

        pkgs = await repo.get_all_installed()
        assert {p.name for p in pkgs} == {"iina", "act", "yazi"}

    async def test_kind_filter_returns_only_formulae(self, repo):
        """Test that get_all_installed returns only formulae when kind_filter is set to FORMULA."""
        pkgs = await repo.get_all_installed(kind_filter=PackageKind.FORMULA)
        assert [p.name for p in pkgs] == ["act", "yazi"]


class TestLiveOutdatedFlip:
    """End-to-end on real data: a stale cache (act looks current at 0.2.88) gets
    reconciled against `brew outdated`, which reports 0.2.89."""

    async def test_cached_path_does_not_yet_flag_act(self, repo):
        """Test that get_all_installed does not flag act as outdated when cached."""
        outdated = await repo.get_outdated(live=False)
        assert outdated == []

    async def test_live_check_flips_act_and_updates_latest(self, repo):
        """Test that get_outdated with live=True flips act to outdated and updates latest_version."""
        outdated = await repo.get_outdated(live=False)
        assert outdated == []

        outdated = await repo.get_outdated(live=True)

        assert {p.name for p in outdated} == {"act"}
        act = outdated[0]
        assert PackageStatus.OUTDATED in act.status

        # latest_version moved from the stale 0.2.88 to the live 0.2.89.
        assert act.metadata["latest_version"] == "0.2.89"

    async def test_flip_persists_to_cache(self, repo):
        """Test that get_outdated with live=True persists the reconciled status to cache."""
        await repo.get_outdated(live=True)

        # A subsequent cached read now reflects the reconciled status.
        cached_outdated = await repo.get_outdated(live=False)
        assert {p.name for p in cached_outdated} == {"act"}

    async def test_non_outdated_packages_keep_clean_status(self, repo):
        """Test that non-outdated packages keep their clean status after live reconciliation."""
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
