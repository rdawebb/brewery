"""Unit tests for the retention cleanup candidates logic."""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from brewery.providers.retention import (
    _stamp_path,
    cleanup_candidates,
    due_for_cleanup,
    mark_cleanup_run,
    mark_replaced,
)

pytestmark = pytest.mark.unit

DAY = 86400
MB = 1024 * 1024


def _keg(cellar, name, version) -> Path:
    """Create a keg at cellar/name/version and return its path.

    Args:
        cellar: The cellar directory.
        name: The keg name.
        version: The keg version.

    Returns:
        The path to the created keg.
    """
    k = cellar / name / version
    k.mkdir(parents=True)

    return k


def _install(cellar, name, version, *, time, replaced_at=None, nbytes=0) -> Path:
    """Install a keg at cellar/name/version with optional sidecar and return its path.

    Args:
        cellar: The cellar directory.
        name: The keg name.
        version: The keg version.
        time: The installation time.
        replaced_at: The time the keg was replaced, if any.
        nbytes: The size of the sidecar, if any.

    Returns:
        The path to the installed keg.
    """
    keg = cellar / name / version
    (keg / "bin").mkdir(parents=True)
    (keg / "INSTALL_RECEIPT.json").write_bytes(orjson.dumps({"time": time}))
    if nbytes:
        (keg / "bin" / "blob").write_bytes(b"\0" * nbytes)

    if replaced_at is not None:
        mark_replaced(keg, by="x", at=replaced_at)

    return keg


class TestCleanupCandidates:
    """Tests for the cleanup_candidates function."""

    def test_old_sidecar_is_candidate(self, tmp_path) -> None:
        """Test that an old sidecar is a candidate for cleanup."""
        cellar = tmp_path / "Cellar"
        mark_replaced(_keg(cellar, "wget", "1.0"), by="2.0", at=1000)
        cands = cleanup_candidates(cellar, active=set(), now=1000 + 31 * DAY)
        assert [(c.name, c.version) for c in cands] == [("wget", "1.0")]

    def test_age_boundary_inclusive(self, tmp_path) -> None:
        """Test that the age boundary is inclusive."""
        cellar = tmp_path / "Cellar"
        mark_replaced(_keg(cellar, "wget", "1.0"), by="2.0", at=1000)

        # now - 30d == replaced_at -> at <= cutoff -> eligible
        assert len(cleanup_candidates(cellar, active=set(), now=1000 + 30 * DAY)) == 1

    def test_recent_sidecar_excluded(self, tmp_path) -> None:
        """Test that a recent sidecar is excluded."""
        cellar = tmp_path / "Cellar"
        mark_replaced(_keg(cellar, "wget", "1.0"), by="2.0", at=1000)
        assert cleanup_candidates(cellar, active=set(), now=1000 + 10 * DAY) == []

    def test_sidecarless_excluded(self, tmp_path) -> None:
        """Test that a sidecarless keg is excluded."""
        cellar = tmp_path / "Cellar"
        _keg(cellar, "wget", "1.0")  # No sidecar
        assert cleanup_candidates(cellar, active=set(), now=1000 + 99 * DAY) == []

    def test_active_excluded(self, tmp_path) -> None:
        """Test that an active keg is excluded."""
        cellar = tmp_path / "Cellar"
        k = _keg(cellar, "wget", "1.0")
        mark_replaced(k, by="2.0", at=1000)
        assert cleanup_candidates(cellar, active={k}, now=1000 + 99 * DAY) == []

    def test_missing_cellar_is_empty(self, tmp_path) -> None:
        """Test that a missing cellar is empty."""
        assert cleanup_candidates(tmp_path / "nope", active=set()) == []


class TestCleanupGate:
    """Tests for the cleanup gate."""

    def test_missing_stamp_is_due(self, tmp_path) -> None:
        """Test that a missing stamp is due for cleanup."""
        assert due_for_cleanup(tmp_path) is True

    def test_recent_stamp_not_due(self, tmp_path) -> None:
        """Test that a recent stamp is not due for cleanup."""
        mark_cleanup_run(tmp_path, at=1000)
        assert due_for_cleanup(tmp_path, now=1000 + 100) is False

    def test_boundary_is_due(self, tmp_path) -> None:
        """Test that the boundary is due for cleanup."""
        mark_cleanup_run(tmp_path, at=1000)
        assert due_for_cleanup(tmp_path, now=1000 + 86400) is True  # >= interval

    def test_corrupt_stamp_is_due(self, tmp_path) -> None:
        """Test that a corrupt stamp is due for cleanup."""
        _stamp_path(tmp_path).write_text("not-a-number")
        assert due_for_cleanup(tmp_path) is True  # ValueError -> due

    def test_mark_round_trip_atomic(self, tmp_path) -> None:
        """Test that mark_cleanup_run is atomic."""
        mark_cleanup_run(tmp_path, at=12345)
        assert _stamp_path(tmp_path).read_text() == "12345"
        assert list(tmp_path.glob("*.tmp")) == []  # No temp left behind


class TestCountCap:
    """Tests for the count cap retention strategy."""

    def test_keeps_active_plus_newest_stale(self, tmp_path) -> None:
        """Test that the count cap retention strategy keeps active plus the newest stale version."""
        cellar = tmp_path / "Cellar"
        active = _install(cellar, "wget", "3.0", time=300)
        _install(cellar, "wget", "2.0", time=200)
        _install(cellar, "wget", "1.0", time=100)
        cands = cleanup_candidates(cellar, active={active}, max_versions=2)
        assert {c.version for c in cands} == {"1.0"}  # Active + 2.0 kept
        assert all(c.reason == "max_versions" for c in cands)

    def test_cap_evicts_sidecarless(self, tmp_path) -> None:
        """Test that the count cap retention strategy evicts sidecarless versions."""
        cellar = tmp_path / "Cellar"
        active = _install(cellar, "wget", "2.0", time=200)
        _install(cellar, "wget", "1.0", time=100)  # No sidecar
        cands = cleanup_candidates(cellar, active={active}, max_versions=1)
        assert {c.version for c in cands} == {"1.0"}  # Cap overrides exemption

    def test_no_cap_leaves_sidecarless(self, tmp_path) -> None:
        """Test that the count cap retention strategy leaves sidecarless versions when no cap is set."""
        cellar = tmp_path / "Cellar"
        active = _install(cellar, "wget", "2.0", time=200)
        _install(cellar, "wget", "1.0", time=100)  # No sidecar, no cap
        assert cleanup_candidates(cellar, active={active}) == []  # Default hands-off


class TestSizeCap:
    """Test size cap retention strategy."""

    def test_evicts_oldest_until_under(self, tmp_path) -> None:
        """Test that the size cap retention strategy evicts the oldest version until under the cap."""
        cellar = tmp_path / "Cellar"
        active = _install(cellar, "wget", "3.0", time=300, nbytes=2 * MB)
        _install(cellar, "wget", "2.0", time=200, nbytes=2 * MB)
        _install(cellar, "wget", "1.0", time=100, nbytes=2 * MB)
        cands = cleanup_candidates(cellar, active={active}, max_cellar_mb=5)
        assert {c.version for c in cands} == {"1.0"}  # 6MB -> drop oldest -> 4MB
        assert all(c.reason == "max_cellar_mb" for c in cands)

    def test_active_over_budget_evicts_all_stale(self, tmp_path) -> None:
        """Test that the size cap retention strategy evicts all stale versions when active is over the budget."""
        cellar = tmp_path / "Cellar"
        active = _install(cellar, "wget", "3.0", time=300, nbytes=6 * MB)
        _install(cellar, "wget", "2.0", time=200, nbytes=2 * MB)
        _install(cellar, "wget", "1.0", time=100, nbytes=2 * MB)
        cands = cleanup_candidates(cellar, active={active}, max_cellar_mb=5)
        assert {c.version for c in cands} == {
            "1.0",
            "2.0",
        }  # All stale, active untouched


class TestComposition:
    """Test composition of retention strategies."""

    def test_union_and_size_credits_prior_removals(self, tmp_path) -> None:
        """Test that the size cap retention strategy evicts all stale versions when active is over the budget."""
        cellar = tmp_path / "Cellar"
        active = _install(cellar, "wget", "4.0", time=400, nbytes=2 * MB)
        _install(cellar, "wget", "1.0", time=100, replaced_at=1000, nbytes=2 * MB)
        _install(cellar, "wget", "2.0", time=200, nbytes=2 * MB)
        _install(cellar, "wget", "3.0", time=300, nbytes=2 * MB)
        cands = cleanup_candidates(
            cellar,
            active={active},
            max_cellar_mb=5,
            now=1000 + 31 * 86400,
        )
        reasons = {c.version: c.reason for c in cands}
        assert reasons["1.0"] == "aged"  # Past age threshold
        assert reasons["2.0"] == "max_cellar_mb"  # One more needed after age frees 1.0
        assert "3.0" not in reasons  # Newest survivor kept
