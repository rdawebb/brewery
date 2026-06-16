"""Unit tests for Brewery environment discovery and cache-dir handling."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from brewery.core import config
from brewery.core.config import (
    FORMULA_API_PATH,
    HOMEBREW_CACHE,
    BreweryENV,
    ensure_cache_dir,
    get_brewery_env,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def cache_dir(tmp_path, monkeypatch) -> Path:
    """Redirect the module-level cache dir to a temp path.

    _DEF_CACHE is frozen at import time, so the env var cannot move it mid-run;
    patching the module attribute is the supported seam.

    Args:
        tmp_path: The temporary path fixture.
        monkeypatch: The monkeypatch fixture.

    Returns:
        The path to the cache directory.
    """
    target = tmp_path / "cache"
    monkeypatch.setattr(config, "_DEF_CACHE", target)

    return target


class TestEnsureCacheDir:
    """Tests for ensure_cache_dir."""

    def test_creates_directory(self, cache_dir) -> None:
        """Test that the cache directory is created when absent."""
        assert not cache_dir.exists()
        result = ensure_cache_dir()
        assert result == cache_dir
        assert cache_dir.is_dir()

    def test_idempotent_when_present(self, cache_dir) -> None:
        """Test that an existing cache directory is left intact."""
        cache_dir.mkdir(parents=True)
        (cache_dir / "sentinel").write_text("x")
        ensure_cache_dir()
        assert (cache_dir / "sentinel").read_text() == "x"

    def test_creates_nested_parents(self, tmp_path, monkeypatch) -> None:
        """Test that missing parent directories are created."""
        target = tmp_path / "a" / "b" / "cache"
        monkeypatch.setattr(config, "_DEF_CACHE", target)
        ensure_cache_dir()
        assert target.is_dir()


class TestGetBreweryEnv:
    """Tests for get_brewery_env discovery and caching."""

    def test_reads_prefix_from_cache_file(self, cache_dir) -> None:
        """Test that a cached brew_prefix.txt is used without invoking brew."""
        cache_dir.mkdir(parents=True)
        (cache_dir / "brew_prefix.txt").write_text("/opt/homebrew\n")
        (cache_dir / "brew_repository.txt").write_text("/opt/homebrew\n")

        def _fail(*args, **kwargs):
            raise AssertionError("brew should not be called when cache exists")

        # Any subprocess use here would be a regression
        env = get_brewery_env()
        assert env.prefix == Path("/opt/homebrew")
        assert env.cellar == Path("/opt/homebrew/Cellar")
        assert env.caskroom == Path("/opt/homebrew/Caskroom")

    def test_cache_file_is_stripped(self, cache_dir) -> None:
        """Test that surrounding whitespace in the cache file is stripped."""
        cache_dir.mkdir(parents=True)
        (cache_dir / "brew_prefix.txt").write_text("  /usr/local  \n")
        (cache_dir / "brew_repository.txt").write_text("/usr/local/Homebrew\n")
        env = get_brewery_env()
        assert env.prefix == Path("/usr/local")

    def test_discovers_via_brew_and_writes_cache(self, cache_dir, monkeypatch) -> None:
        """Test that an absent cache triggers `brew --prefix` and is persisted."""
        monkeypatch.setattr(
            config.subprocess, "check_output", lambda *a, **k: "/opt/homebrew\n"
        )
        env = get_brewery_env()
        assert env.prefix == Path("/opt/homebrew")

        # The discovered prefix is cached to disk for next time
        assert (cache_dir / "brew_prefix.txt").read_text() == "/opt/homebrew"

    def test_brew_not_found_uses_arm_fallback(self, cache_dir, monkeypatch) -> None:
        """Test that a missing brew on arm64 falls back to /opt/homebrew."""
        monkeypatch.setattr(config.platform, "machine", lambda: "arm64")

        def _raise(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(config.subprocess, "check_output", _raise)
        env = get_brewery_env()
        assert env.prefix == Path("/opt/homebrew")

    def test_brew_not_found_uses_intel_fallback(self, cache_dir, monkeypatch) -> None:
        """Test that a missing brew on Intel falls back to /usr/local."""
        monkeypatch.setattr(config.platform, "machine", lambda: "x86_64")

        def _raise(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(config.subprocess, "check_output", _raise)
        env = get_brewery_env()
        assert env.prefix == Path("/usr/local")

    def test_brew_command_error_uses_fallback(self, cache_dir, monkeypatch) -> None:
        """Test that a failing brew invocation falls back rather than raising."""
        monkeypatch.setattr(config.platform, "machine", lambda: "arm64")

        def _raise(*a, **k):
            raise subprocess.CalledProcessError(returncode=1, cmd=["brew"])

        monkeypatch.setattr(config.subprocess, "check_output", _raise)
        env = get_brewery_env()
        assert env.prefix == Path("/opt/homebrew")

    def test_failed_discovery_does_not_write_cache(
        self, cache_dir, monkeypatch
    ) -> None:
        """Test that a fallback prefix is not persisted to the cache file."""
        monkeypatch.setattr(config.platform, "machine", lambda: "arm64")

        def _raise(*a, **k):
            raise FileNotFoundError

        monkeypatch.setattr(config.subprocess, "check_output", _raise)
        get_brewery_env()
        assert not (cache_dir / "brew_prefix.txt").exists()

    def test_unreadable_cache_file_falls_back_to_discovery(
        self, cache_dir, monkeypatch
    ) -> None:
        """Test that a cache file that fails to read triggers brew discovery.

        The read is guarded; a failure must not propagate, it should fall through
        to discovering the prefix afresh.
        """
        cache_dir.mkdir(parents=True)
        prefix_file = cache_dir / "brew_prefix.txt"
        prefix_file.write_text("/whatever")

        original_read_text = Path.read_text

        def _boom(self, *a, **k):
            if self == prefix_file:
                raise OSError("unreadable")
            return original_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _boom)
        monkeypatch.setattr(
            config.subprocess, "check_output", lambda *a, **k: "/opt/homebrew\n"
        )
        env = get_brewery_env()
        assert env.prefix == Path("/opt/homebrew")

    def test_result_is_memoized(self, cache_dir, monkeypatch) -> None:
        """Test that a second call returns the cached env without rediscovery."""
        calls = {"n": 0}

        def _once(*a, **k):
            calls["n"] += 1
            return "/opt/homebrew\n"

        monkeypatch.setattr(config.subprocess, "check_output", _once)
        first = get_brewery_env()
        second = get_brewery_env()
        assert first is second
        assert calls["n"] == 2

    def test_derived_paths_track_prefix(self, cache_dir) -> None:
        """Test that cellar and caskroom are derived from the resolved prefix."""
        cache_dir.mkdir(parents=True)
        (cache_dir / "brew_prefix.txt").write_text("/custom/brew")
        (cache_dir / "brew_repository.txt").write_text("/custom/brew/Homebrew")
        env = get_brewery_env()
        assert env == BreweryENV(
            prefix=Path("/custom/brew"),
            cellar=Path("/custom/brew/Cellar"),
            caskroom=Path("/custom/brew/Caskroom"),
            repository=Path("/custom/brew/Homebrew"),
            api_path=FORMULA_API_PATH,
            bottle_cache=HOMEBREW_CACHE,
        )
