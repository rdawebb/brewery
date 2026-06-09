"""Integration tests for the file cache, CacheManager, and size attachment."""

from __future__ import annotations
from pathlib import Path

import orjson
import pytest

from brewery.core import cache as cache_mod
from brewery.core import fs_state as fs_mod
from brewery.core.cache import Cache, CacheManager
from brewery.core.config import BreweryENV
from brewery.core.fs_state import attach_sizes
from brewery.core.models import InstalledRecord, PackageKind

pytestmark = pytest.mark.integration


class TestCacheTokenRoundTrip:
    """Tests for token-validated get/set on the file cache."""

    def test_set_then_get_hits(self, fake_env) -> None:
        """Test that a value set and read under a stable env is a cache hit."""
        c = Cache(namespace="t1")
        c.set("k", {"a": 1})
        assert c.get("k") == {"a": 1}

    def test_missing_key_returns_none(self, fake_env) -> None:
        """Test that an unknown key returns None."""
        assert Cache(namespace="t2").get("absent") is None

    def test_corrupt_file_returns_none(self, fake_env) -> None:
        """Test that an unparseable cache file reads back as None, not an error."""
        c = Cache(namespace="t3")
        c._file("k").write_text("{not json")
        assert c.get("k") is None

    def test_token_change_invalidates(self, fake_env) -> None:
        """Test that a changed filesystem token misses the cached value.

        The token is derived from Cellar/Caskroom/Taps mtimes; touching the
        Cellar after a set means the stored token no longer matches on read.
        """
        c = Cache(namespace="t4")
        c.set("k", "v")

        # Force a new mtime on the cellar, then drop to force recompute
        (fake_env.cellar / "newpkg").mkdir()
        c.invalidate_token()
        assert c.get("k") is None

    def test_delete_removes_value(self, fake_env) -> None:
        """Test that delete removes a cached entry."""
        c = Cache(namespace="t5")
        c.set("k", "v")
        c.delete("k")
        assert c.get("k") is None

    def test_delete_missing_is_silent(self, fake_env) -> None:
        """Test that deleting an absent key does not raise."""
        Cache(namespace="t6").delete("absent")  # No exception


class TestCacheManagerRecords:
    """Tests for installed-record caching and invalidation."""

    def _manager(self, catalog, fake_env) -> CacheManager:
        """Create a CacheManager for testing.

        Args:
            catalog: The catalog to use.
            fake_env: The fake environment to use.

        Returns:
            A CacheManager instance.
        """
        return CacheManager(Cache(namespace="repository"), catalog, env=fake_env)

    def test_records_scanned_then_cached(
        self, catalog, fake_env, mock_brew, monkeypatch
    ) -> None:
        """Test that a second read is served from cache without rescanning.

        After the first scan caches records, monkeypatching the scanner to raise
        proves the second read never touches the filesystem.
        """
        mgr = self._manager(catalog, fake_env)
        first = mgr.installed_records()
        assert {r.name for r in first} == {"yazi", "act", "iina"}

        def _boom(env: BreweryENV | None = None) -> list[InstalledRecord]:
            raise AssertionError("scan_installed should not run on a cache hit")

        monkeypatch.setattr(cache_mod, "scan_installed", _boom)
        second = mgr.installed_records()
        assert {r.name for r in second} == {"yazi", "act", "iina"}

    def test_invalidate_forces_rescan(self, catalog, fake_env, mock_brew) -> None:
        """Test that invalidate drops the records key so the next read rescans."""
        mgr = self._manager(catalog, fake_env)
        mgr.installed_records()
        mgr.invalidate()

        # A new keg appears, so after invalidation the rescan should see it
        keg = fake_env.cellar / "ripgrep" / "14.1.0"
        keg.mkdir(parents=True)
        names = {r.name for r in mgr.installed_records()}
        assert "ripgrep" in names

    def test_installed_packages_sorted_by_kind_then_name(
        self, catalog, fake_env, mock_brew
    ) -> None:
        """Test that merged packages are ordered by kind value, then name."""
        mgr = self._manager(catalog, fake_env)
        pkgs = mgr.installed_packages()
        ordered = [(p.kind.value, p.name) for p in pkgs]
        assert ordered == sorted(ordered)

    def test_kind_filter(self, catalog, fake_env, mock_brew) -> None:
        """Test that a kind filter returns only matching packages."""
        mgr = self._manager(catalog, fake_env)
        casks = mgr.installed_packages(kind=PackageKind.CASK)
        assert {p.name for p in casks} == {"iina"}

    def test_find_installed_hit(self, catalog, fake_env, mock_brew) -> None:
        """Test that find_installed returns the single merged package."""
        mgr = self._manager(catalog, fake_env)
        pkg = mgr.find_installed("yazi")
        assert pkg is not None and pkg.name == "yazi"

    def test_find_installed_miss(self, catalog, fake_env, mock_brew) -> None:
        """Test that find_installed returns None for a non-installed name."""
        mgr = self._manager(catalog, fake_env)
        assert mgr.find_installed("ripgrep") is None


def _record(name: str, path: str | None) -> InstalledRecord:
    """Create a new InstalledRecord.

    Args:
        name: The name of the package.
        path: The installation path of the package.

    Returns:
        An InstalledRecord instance.
    """
    return InstalledRecord(
        name=name, kind=PackageKind.FORMULA, version="1.0", path=path
    )


class TestAttachSizes:
    """Tests for size attachment, du batching, and the size cache."""

    @pytest.fixture
    def kegs(self, tmp_path) -> tuple[Path, Path]:
        """Two real keg directories on disk to size.

        Args:
            tmp_path: The temporary directory path fixture.

        Returns:
            A tuple containing the paths to the two keg directories.
        """
        a = tmp_path / "a" / "1.0"
        b = tmp_path / "b" / "1.0"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / "file").write_bytes(b"x" * 2048)
        (b / "file").write_bytes(b"y" * 4096)

        return a, b

    def test_sizes_measured_and_attached(self, kegs, tmp_path) -> None:
        """Test that du-measured sizes are attached to records."""
        a, b = kegs
        records = [_record("a", str(a)), _record("b", str(b))]
        attach_sizes(records, cache_dir=tmp_path / "cache")
        sizes = {r.name: r.size_kb for r in records}
        assert sizes["a"] is not None and sizes["a"] > 0
        assert sizes["b"] is not None and sizes["b"] > 0

    def test_size_cache_written(self, kegs, tmp_path) -> None:
        """Test that measured sizes are persisted to the size cache file."""
        a, _ = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        attach_sizes([_record("a", str(a))], cache_dir=cache_dir)
        data = orjson.loads((cache_dir / "keg_sizes.json").read_bytes())
        assert "a" in data

    def test_cache_hit_skips_measurement(self, kegs, tmp_path, monkeypatch) -> None:
        """Test that an unchanged keg reuses the cached size without calling du.

        A second attach with the same keg mtime must serve from the size cache;
        patching subprocess.run to fail proves du is not invoked.
        """
        a, _ = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        rec = _record("a", str(a))
        attach_sizes([rec], cache_dir=cache_dir)
        cached_size = rec.size_kb

        def _boom(*a, **k):
            raise AssertionError("du should not run on a size-cache hit")

        monkeypatch.setattr(fs_mod.subprocess, "run", _boom)
        rec2 = _record("a", str(a))
        attach_sizes([rec2], cache_dir=cache_dir)
        assert rec2.size_kb == cached_size

    def test_stale_mtime_remeasures(self, kegs, tmp_path) -> None:
        """Test that a changed keg mtime triggers a fresh measurement.

        Modifying the keg after caching invalidates the entry by mtime, so the
        size is measured again rather than reused.
        """
        a, _ = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        attach_sizes([_record("a", str(a))], cache_dir=cache_dir)

        # Grow the keg and bump its mtime
        (a / "more").write_bytes(b"z" * 8192)
        import os
        import time

        os.utime(a, (time.time() + 10, time.time() + 10))
        rec2 = _record("a", str(a))
        attach_sizes([rec2], cache_dir=cache_dir)
        assert rec2.size_kb is not None

    def test_records_without_path_skipped(self, tmp_path) -> None:
        """Test that records lacking a path are left unsized without error."""
        rec = _record("nopath", None)
        attach_sizes([rec], cache_dir=tmp_path / "cache")
        assert rec.size_kb is None

    def test_uninstalled_dropped_from_cache(self, kegs, tmp_path) -> None:
        """Test that the rebuilt size cache drops packages no longer present.

        attach_sizes rebuilds the cache from current records, so a package sized
        in one run but absent in the next is pruned from the cache file.
        """
        a, b = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        attach_sizes([_record("a", str(a)), _record("b", str(b))], cache_dir=cache_dir)

        # Second run includes only "a"
        attach_sizes([_record("a", str(a))], cache_dir=cache_dir)
        data = orjson.loads((cache_dir / "keg_sizes.json").read_bytes())
        assert "a" in data
        assert "b" not in data

    def test_du_failure_leaves_size_none(self, kegs, tmp_path, monkeypatch) -> None:
        """Test that a du spawn failure leaves sizes unset rather than raising."""
        a, _ = kegs

        def _fail(*a, **k) -> None:
            """Simulate a failure in subprocess.run.

            Args:
                *a: Positional arguments.
                **k: Keyword arguments.
            """
            raise OSError("spawn failed")

        monkeypatch.setattr(fs_mod.subprocess, "run", _fail)
        rec = _record("a", str(a))
        attach_sizes([rec], cache_dir=tmp_path / "cache")
        assert rec.size_kb is None

    def test_corrupt_size_cache_recovered(self, kegs, tmp_path) -> None:
        """Test that a corrupt size-cache file is treated as empty, not fatal."""
        a, _ = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "keg_sizes.json").write_text("{not json")
        rec = _record("a", str(a))
        attach_sizes([rec], cache_dir=cache_dir)
        assert rec.size_kb is not None
