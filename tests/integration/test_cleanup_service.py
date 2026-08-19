"""Integration tests for the cleanup service's retention sweep."""

from __future__ import annotations

import time

import pytest

from brewery.core import config
from brewery.core.repo import Repository
from brewery.providers.retention import mark_replaced
from brewery.services.cleanup import cleanup_packages

pytestmark = pytest.mark.integration


class TestCleanup:
    """Tests for the cleanup service."""

    async def test_cleanup_removes_old_keeps_active_and_recent(
        self, brew, empty_catalog, monkeypatch
    ) -> None:
        """Test that cleanup removes old stale versions, keeps active and recent versions."""
        DAY = 86400
        brew.formula(
            "wget",
            "2.0",
            receipt={"source": {"tap": "homebrew/core"}, "runtime_dependencies": []},
            link_opt=True,  # opt -> 2.0, so fs_state marks it active
        )
        monkeypatch.setattr(config, "_env_cache", brew.env)

        cellar = brew.cellar
        old = cellar / "wget" / "1.0"
        old.mkdir(parents=True)
        recent = cellar / "wget" / "3.0"
        recent.mkdir(parents=True)
        now = int(time.time())
        mark_replaced(old, by="2.0", at=now - 40 * DAY)
        mark_replaced(recent, by="2.0", at=now - 2 * DAY)

        removed, failures = await cleanup_packages(Repository(catalog=empty_catalog))

        assert removed == ["wget 1.0"]
        assert failures == []
        assert not old.exists()  # Old stale: removed
        assert recent.exists()  # Recent stale: kept
        assert (cellar / "wget" / "2.0").exists()  # Active: kept

    async def test_cleanup_removes_every_stale_keg_of_one_rack(
        self, brew, empty_catalog, monkeypatch
    ) -> None:
        """Test that several stale kegs of one formula are all removed.

        The rack lock is per formula and not reentrant across threads, so a sweep
        that parallelised per keg rather than per rack would find its own sibling
        holding the lock and silently skip it.
        """
        DAY = 86400
        brew.formula(
            "wget",
            "3.0",
            receipt={"source": {"tap": "homebrew/core"}, "runtime_dependencies": []},
            link_opt=True,
        )
        monkeypatch.setattr(config, "_env_cache", brew.env)

        now = int(time.time())
        stale = []
        for version in ("1.0", "1.5", "2.0"):
            keg = brew.cellar / "wget" / version
            keg.mkdir(parents=True)
            mark_replaced(keg, by="3.0", at=now - 40 * DAY)
            stale.append(keg)

        removed, failures = await cleanup_packages(Repository(catalog=empty_catalog))

        assert failures == []
        assert sorted(removed) == ["wget 1.0", "wget 1.5", "wget 2.0"]
        assert not any(keg.exists() for keg in stale)
        assert (brew.cellar / "wget" / "3.0").exists()

    async def test_cleanup_sweeps_several_racks(
        self, brew, empty_catalog, monkeypatch
    ) -> None:
        """Test that stale kegs across racks are all removed, one lock per rack."""
        DAY = 86400
        monkeypatch.setattr(config, "_env_cache", brew.env)

        now = int(time.time())
        stale = []
        for name in ("wget", "curl", "jq"):
            brew.formula(
                name,
                "2.0",
                receipt={
                    "source": {"tap": "homebrew/core"},
                    "runtime_dependencies": [],
                },
                link_opt=True,
            )
            keg = brew.cellar / name / "1.0"
            keg.mkdir(parents=True)
            mark_replaced(keg, by="2.0", at=now - 40 * DAY)
            stale.append(keg)

        removed, failures = await cleanup_packages(Repository(catalog=empty_catalog))

        assert failures == []
        assert sorted(removed) == ["curl 1.0", "jq 1.0", "wget 1.0"]
        assert not any(keg.exists() for keg in stale)

    async def test_cleanup_skips_a_locked_rack(
        self, brew, empty_catalog, monkeypatch
    ) -> None:
        """Test that a rack mid-install is left for the next sweep, not reported as a failure."""
        import fcntl
        import os

        from brewery.core.locks import lock_path

        brew.formula(
            "wget",
            "2.0",
            receipt={"source": {"tap": "homebrew/core"}, "runtime_dependencies": []},
            link_opt=True,
        )
        monkeypatch.setattr(config, "_env_cache", brew.env)

        old = brew.cellar / "wget" / "1.0"
        old.mkdir(parents=True)
        mark_replaced(old, by="2.0", at=int(time.time()) - 40 * 86400)

        path = lock_path(brew.env.prefix, "wget")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            removed, failures = await cleanup_packages(
                Repository(catalog=empty_catalog)
            )

        finally:
            os.close(fd)

        assert removed == []
        assert failures == []  # Opportunistic: the daemon retries tomorrow
        assert old.exists()
