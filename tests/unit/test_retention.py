"""Unit tests for the retention cleanup candidates logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from brewery.providers.retention import cleanup_candidates, mark_replaced

pytestmark = pytest.mark.unit

DAY = 86400


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
