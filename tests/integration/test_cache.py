"""Integration tests for the file cache, CacheManager, and size attachment."""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from brewery.core import cache as cache_mod
from brewery.core import keg_sizes as keg_sizes_mod
from brewery.core.cache import Cache, CacheManager
from brewery.core.config import BreweryENV
from brewery.core.keg_sizes import attach_sizes
from brewery.core.models import InstalledRecord, PackageKind


class TestCacheTokenRoundTrip:
    """Tests for token-validated get/set on the file cache."""

    def test_set_then_get_hits(self, mock_env) -> None:
        """Test that a value set and read under a stable env is a cache hit."""
        c = Cache(namespace="t1")
        c.set("k", {"a": 1})
        assert c.get("k") == {"a": 1}

    def test_missing_key_returns_none(self, mock_env) -> None:
        """Test that an unknown key returns None."""
        assert Cache(namespace="t2").get("absent") is None

    def test_corrupt_file_returns_none(self, mock_env) -> None:
        """Test that an unparseable cache file reads back as None, not an error."""
        c = Cache(namespace="t3")
        c._file("k").write_text("{not json")
        assert c.get("k") is None

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"[]", id="list"),
            pytest.param(b'"just a string"', id="string"),
            pytest.param(b"3", id="number"),
            pytest.param(b"null", id="null"),
        ],
    )
    def test_non_object_payload_returns_none(self, mock_env, payload) -> None:
        """Test that valid JSON of the wrong shape is a miss, not a CacheError."""
        c = Cache(namespace="t3-shape")
        c._file("k").write_bytes(payload)
        assert c.get("k") is None

    def test_token_change_invalidates(self, mock_env) -> None:
        """Test that a changed filesystem token misses the cached value.

        Adding a direct child of the Cellar after a set means the stored token
        no longer matches on read.
        """
        c = Cache(namespace="t4")
        c.set("k", "v")

        (mock_env.cellar / "newpkg").mkdir()
        assert c.get("k") is None

    def test_delete_removes_value(self, mock_env) -> None:
        """Test that delete removes a cached entry."""
        c = Cache(namespace="t5")
        c.set("k", "v")
        c.delete("k")
        assert c.get("k") is None

    def test_delete_missing_is_silent(self, mock_env) -> None:
        """Test that deleting an absent key does not raise."""
        Cache(namespace="t6").delete("absent")  # No exception

    @pytest.mark.parametrize(
        "bookkeeping_dir",
        [
            pytest.param("var/homebrew/pinned", id="pinned"),
            pytest.param("var/homebrew/linked", id="linked"),
            pytest.param("Library/PinnedKegs", id="pinned_legacy"),
            pytest.param("Library/LinkedKegs", id="linked_legacy"),
        ],
    )
    def test_pin_and_link_bookkeeping_invalidates(
        self, mock_env, bookkeeping_dir
    ) -> None:
        """Test that writing a pin/link record misses the cached value.

        `brew pin foo` only writes `var/homebrew/pinned/foo`; it touches
        neither the Cellar nor the Caskroom. If those dirs are absent from the
        token, brewery keeps serving stale pin/link state.
        """
        directory: Path = mock_env.prefix / bookkeeping_dir
        directory.mkdir(parents=True)

        c = Cache(namespace=f"tok-{bookkeeping_dir.replace('/', '-')}")
        c.set("k", "v")
        assert c.get("k") == "v"

        # Equivalent of `brew pin foo` / `brew link foo`: a new direct child.
        (directory / "foo").symlink_to(mock_env.cellar / "foo" / "1.0")
        assert c.get("k") is None

    def test_token_survives_absent_bookkeeping_dirs(self, mock_env) -> None:
        """Test that a prefix with no pin/link dirs still round-trips.

        A fresh prefix has neither directory; a missing path must contribute a
        stable value to the token rather than raising or churning it.
        """
        c = Cache(namespace="t7")
        c.set("k", "v")
        assert c.get("k") == "v"


class TestCacheManagerRecords:
    """Tests for installed-record caching and invalidation."""

    def _manager(self, catalog, mock_env) -> CacheManager:
        """Create a CacheManager for testing.

        Args:
            catalog: The catalog to use.
            mock_env: The mock environment to use.

        Returns:
            A CacheManager instance.
        """
        return CacheManager(Cache(namespace="repository"), catalog, env=mock_env)

    def test_records_scanned_then_cached(
        self, catalog, mock_env, mock_brew, monkeypatch
    ) -> None:
        """Test that a second read is served from cache without rescanning.

        After the first scan caches records, monkeypatching the scanner to raise
        proves the second read never touches the filesystem.
        """
        mgr = self._manager(catalog, mock_env)
        first = mgr.installed_records()
        assert {r.name for r in first} == {"yazi", "act", "iina"}

        def _boom(env: BreweryENV | None = None) -> list[InstalledRecord]:
            raise AssertionError("scan_installed should not run on a cache hit")

        monkeypatch.setattr(cache_mod, "scan_installed", _boom)
        second = mgr.installed_records()
        assert {r.name for r in second} == {"yazi", "act", "iina"}

    def test_invalidate_forces_rescan(self, catalog, mock_env, mock_brew) -> None:
        """Test that invalidate drops the records key so the next read rescans."""
        mgr = self._manager(catalog, mock_env)
        mgr.installed_records()
        mgr.invalidate()

        # A new keg appears, so after invalidation the rescan should see it
        keg = mock_env.cellar / "ripgrep" / "14.1.0"
        keg.mkdir(parents=True)
        names = {r.name for r in mgr.installed_records()}
        assert "ripgrep" in names

    @pytest.mark.parametrize(
        "cached",
        [
            pytest.param([{"kind": "formula", "version": "1.0"}], id="missing_name"),
            pytest.param([{"name": "yazi", "kind": "nonsense"}], id="bad_kind"),
            pytest.param(["not-a-dict"], id="not_a_dict"),
        ],
    )
    def test_unreadable_records_rescan(
        self, catalog, mock_env, mock_brew, cached
    ) -> None:
        """Test that an unrebuildable cached payload rescans instead of raising.

        A record dict written by an older schema (or a truncated one) must be
        treated as a miss; otherwise every command touching installed state
        dies on a KeyError with no way back short of deleting the cache.
        """
        mgr = self._manager(catalog, mock_env)
        mgr.cache.set(CacheManager._RECORDS_KEY, cached)

        records = mgr.installed_records()
        assert {r.name for r in records} == {"yazi", "act", "iina"}

    def test_installed_packages_sorted_by_kind_then_name(
        self, catalog, mock_env, mock_brew
    ) -> None:
        """Test that merged packages are ordered by kind value, then name."""
        mgr = self._manager(catalog, mock_env)
        pkgs = mgr.installed_packages()
        ordered = [(p.kind.value, p.name) for p in pkgs]
        assert ordered == sorted(ordered)

    def test_kind_filter(self, catalog, mock_env, mock_brew) -> None:
        """Test that a kind filter returns only matching packages."""
        mgr = self._manager(catalog, mock_env)
        casks = mgr.installed_packages(kind=PackageKind.CASK)
        assert {p.name for p in casks} == {"iina"}

    def test_find_installed_hit(self, catalog, mock_env, mock_brew) -> None:
        """Test that find_installed returns the single merged package."""
        mgr = self._manager(catalog, mock_env)
        pkg = mgr.find_installed("yazi")
        assert pkg is not None and pkg.name == "yazi"

    def test_find_installed_miss(self, catalog, mock_env, mock_brew) -> None:
        """Test that find_installed returns None for a non-installed name."""
        mgr = self._manager(catalog, mock_env)
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

        monkeypatch.setattr(keg_sizes_mod.subprocess, "run", _boom)
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
        """Test that a keg no longer on disk is pruned from the cache file."""
        import shutil

        a, b = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        attach_sizes([_record("a", str(a)), _record("b", str(b))], cache_dir=cache_dir)

        # "b" is uninstalled, so the next run has nothing to keep its entry for
        shutil.rmtree(b)
        attach_sizes([_record("a", str(a))], cache_dir=cache_dir)
        data = orjson.loads((cache_dir / "keg_sizes.json").read_bytes())
        assert "a" in data
        assert "b" not in data

    def test_a_package_absent_from_the_records_is_kept(self, kegs, tmp_path) -> None:
        """Test that sizing a subset does not evict the packages it leaves out."""
        a, b = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        attach_sizes([_record("a", str(a)), _record("b", str(b))], cache_dir=cache_dir)

        attach_sizes([_record("a", str(a))], cache_dir=cache_dir)
        data = orjson.loads((cache_dir / "keg_sizes.json").read_bytes())
        assert "b" in data, "a keg that is still installed was evicted"

    def test_an_empty_record_list_keeps_the_cache(self, kegs, tmp_path) -> None:
        """Test that a scan returning nothing does not empty the cache file."""
        a, b = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        attach_sizes([_record("a", str(a)), _record("b", str(b))], cache_dir=cache_dir)

        attach_sizes([], cache_dir=cache_dir)
        data = orjson.loads((cache_dir / "keg_sizes.json").read_bytes())
        assert set(data) == {"a", "b"}

    def test_du_failure_does_not_evict_the_previous_size(
        self, kegs, tmp_path, monkeypatch
    ) -> None:
        """Test that a keg whose re-measure failed keeps its old cache entry."""
        import os
        import time

        a, _ = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        attach_sizes([_record("a", str(a))], cache_dir=cache_dir)

        # Invalidate by mtime so the next call has to measure, then break du
        os.utime(a, (time.time() + 10, time.time() + 10))

        def _fail(*args, **kwargs) -> None:
            """Simulate du failing to spawn."""
            raise OSError("spawn failed")

        monkeypatch.setattr(keg_sizes_mod.subprocess, "run", _fail)
        attach_sizes([_record("a", str(a))], cache_dir=cache_dir)

        data = orjson.loads((cache_dir / "keg_sizes.json").read_bytes())
        assert "a" in data

    def test_size_cache_is_written_atomically(
        self, kegs, tmp_path, monkeypatch
    ) -> None:
        """Test that a failed write leaves the previous cache intact."""
        a, b = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        attach_sizes([_record("a", str(a)), _record("b", str(b))], cache_dir=cache_dir)
        before = (cache_dir / "keg_sizes.json").read_bytes()

        real_write = Path.write_bytes

        def _write_then_fail(self, data) -> None:
            """Write, then fail as a crash between truncating and finishing would.

            Args:
                self: The path being written.
                data: The bytes to write.
            """
            real_write(self, data)
            raise OSError("no space left on device")

        monkeypatch.setattr(Path, "write_bytes", _write_then_fail)
        keg_sizes_mod._save_size_cache(cache_dir=cache_dir, data={"c": [1, 2, "/c"]})

        assert (cache_dir / "keg_sizes.json").read_bytes() == before
        assert not list(cache_dir.glob("*.tmp"))

    def test_an_older_schema_entry_is_still_read(self, kegs, tmp_path) -> None:
        """Test that a two-field entry from an earlier version still hits."""
        a, _ = kegs
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        mtime_ns = a.stat().st_mtime_ns
        (cache_dir / "keg_sizes.json").write_bytes(
            orjson.dumps({"a": [mtime_ns, 4242]})
        )

        rec = _record("a", str(a))
        attach_sizes([rec], cache_dir=cache_dir)
        assert rec.size_kb == 4242

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

        monkeypatch.setattr(keg_sizes_mod.subprocess, "run", _fail)
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
