"""Unit tests for running a cask's uninstall directives as brew does."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import brewery.providers.cask_directives as directives
from brewery.core.errors import CellarError


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A temp home dir, so `~` and brew's home undeletables resolve under it."""
    monkeypatch.setenv("HOME", str(tmp_path))

    return tmp_path


class TestResolve:
    """Tests for resolving directive paths as brew's `each_resolved_path` does."""

    def test_globs_braces_and_home(self, home) -> None:
        """Test that `~`, `*` and `{a,b}` expand, and a missing path matches nothing."""
        for name in ("a.plist", "b.plist", "c.log"):
            (home / name).write_text("")

        assert list(
            directives._resolve("trash", ["~/*.plist", "~/{c,d}.log", "~/no"])
        ) == [
            [home / "a.plist", home / "b.plist"],
            [home / "c.log"],
            [],
        ]

    @pytest.mark.parametrize("path", ["relative/x", "/tmp/../etc", "~/./x"])
    def test_skips_unsafe_paths(self, home, path) -> None:
        """Test that relative paths and `.`/`..` segments are skipped outright."""
        assert list(directives._resolve("delete", [path])) == []

    def test_skips_undeletable(self, home) -> None:
        """Test that a glob reaching one of brew's protected dirs leaves it out."""
        for name in ("Library", "Library/Caches", "Library/Caches/foo"):
            (home / name).mkdir()

        assert list(directives._resolve("delete", ["~/Library/*", "~/*"])) == [[], []]
        assert list(directives._resolve("delete", ["~/Library/Caches/*"])) == [
            [home / "Library" / "Caches" / "foo"]
        ]

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/", True),
            ("/Library", True),
            ("~", True),
            ("~/Desktop", True),
            ("~/x", False),
        ],
    )
    def test_undeletable(self, home, path, expected) -> None:
        """Test brew's system and home lists."""
        path = path.replace("~", str(home))
        assert directives.undeletable(Path(path)) is expected


class TestRmdir:
    """Tests for removing dirs only while they hold nothing but empty dirs."""

    def test_removes_an_empty_tree(self, tmp_path) -> None:
        """Test that nested empty dirs and a `.DS_Store` don't keep a dir."""
        (tmp_path / "app" / "a" / "b").mkdir(parents=True)
        (tmp_path / "app" / ".DS_Store").write_text("")

        directives._rmdir([str(tmp_path / "app")])

        assert not (tmp_path / "app").exists()

    def test_keeps_a_dir_holding_a_file(self, tmp_path) -> None:
        """Test that a file anywhere inside keeps the dirs above it."""
        (tmp_path / "app" / "a").mkdir(parents=True)
        (tmp_path / "app" / "a" / "keep").write_text("")

        directives._rmdir([str(tmp_path / "app")])

        assert (tmp_path / "app" / "a" / "keep").exists()


class TestLaunchctl:
    """Tests for removing launchd services."""

    def _launchctl(self, monkeypatch, *, system: bool) -> list[tuple[str, ...]]:
        """Fake `launchctl`, with the service in the user domain or the system's.

        Returns:
            The commands run.
        """
        calls: list[tuple[str, ...]] = []

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            found = args[1] == "remove" or (
                args[1] == "print" and args[2].startswith("system/") == system
            )
            return subprocess.CompletedProcess(args, 0 if found else 113, "", "")

        monkeypatch.setattr(directives, "_run", run)
        monkeypatch.setattr(directives.time, "sleep", lambda _: None)
        return calls

    def test_user_service_is_removed(self, home, monkeypatch) -> None:
        """Test that a service in the user's domain is removed, with its plist."""
        calls = self._launchctl(monkeypatch, system=False)
        plist = home / "Library" / "LaunchAgents" / "org.example.foo.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("")

        directives._launchctl(["org.example.foo"])

        assert ("launchctl", "remove", "org.example.foo") in calls
        assert not plist.exists()

    def test_system_service_needs_brew(self, monkeypatch) -> None:
        """Test that a system-domain service raises, so brew removes it with sudo."""
        calls = self._launchctl(monkeypatch, system=True)

        with pytest.raises(CellarError, match="needs root"):
            directives._launchctl(["org.example.foo"])

        assert not [c for c in calls if c[1] == "remove"]


@pytest.mark.parametrize("values", [["NOPE", "org.example.foo"], ["TERM"]])
def test_bad_signal_directive_needs_brew(values) -> None:
    """Test that an unknown or unpaired signal raises, so the cask goes to brew."""
    with pytest.raises(CellarError, match="bad signal directive"):
        directives._signal(values)
