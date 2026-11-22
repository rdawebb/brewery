"""Unit tests for running a cask's uninstall directives as brew does."""

from __future__ import annotations

import getpass
import os
import plistlib
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

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
            ("~/Library/Keychains/login.keychain-db", True),
            ("~/x", False),
        ],
    )
    def test_undeletable(self, home, path, expected) -> None:
        """Test brew's system and home lists, and its protected containers."""
        path = path.replace("~", str(home))
        assert directives.undeletable(Path(path)) is expected

    def test_undeletable_resolves_the_parent(self, tmp_path, monkeypatch) -> None:
        """Test that a protected dir reached through a symlinked parent is kept."""
        home = tmp_path.resolve() / "home"
        (home / "Desktop").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        (tmp_path / "link").symlink_to(home)

        assert directives.undeletable(tmp_path / "link" / "Desktop")
        assert not directives.undeletable(tmp_path / "link" / "x")


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


_RUN = subprocess.run  # The real one, for test sudo commands run as the user


@pytest.fixture
def sudo_calls(monkeypatch) -> list[tuple[str, ...]]:
    """Record each sudo command, running the `xargs` ones as the user.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The commands, in order.
    """
    calls: list[tuple[str, ...]] = []

    def run_sudo(*args: str, stdin: bytes = b"", check: bool = True) -> None:
        calls.append(args)
        if args[0] == "/usr/bin/xargs":
            proc = _RUN(args, input=stdin, capture_output=True, check=False)
            assert not check or not proc.returncode, proc.stderr

    monkeypatch.setattr(directives, "run_sudo", run_sudo)

    return calls


class TestPkgutil:
    """Tests for removing an installed package's files, as brew's `Cask::Pkg` does."""

    def test_removes_files_and_emptied_dirs_then_forgets(
        self, tmp_path, monkeypatch, sudo_calls
    ) -> None:
        """Test that only the BOM's files go, with dirs they leave empty, then forget."""
        root = tmp_path / "root"
        (root / "Foo.app" / "Contents").mkdir(parents=True)
        (root / "Foo.app" / "Contents" / "foo").write_text("")
        (root / "Foo.app" / ".DS_Store").write_text("")
        (root / "lib").mkdir()
        (root / "lib" / "link").symlink_to("../Foo.app")
        (root / "lib" / "dangling").symlink_to("gone")  # Not in the BOM
        (root / "shared").mkdir()
        (root / "shared" / "mine").write_text("")  # Not in the BOM
        (tmp_path / "escape").write_text("")
        bom = [
            "Foo.app",
            "Foo.app/Contents",
            "Foo.app/Contents/foo",
            "lib",
            "lib/link",
            "shared",
            "../escape",
            "missing",
        ]
        receipt = {
            "volume": "/",
            "install-location": str(root),
            "paths": {p: {} for p in bom},
        }

        def run(cmd: list[str], **_: object) -> subprocess.CompletedProcess:
            assert cmd == ["pkgutil", "--export-plist", "org.example.foo"]
            return subprocess.CompletedProcess(cmd, 0, plistlib.dumps(receipt), b"")

        monkeypatch.setattr(
            directives,
            "_run",
            lambda *a: subprocess.CompletedProcess(a, 0, "org.example.foo\n", ""),
        )
        monkeypatch.setattr(directives.subprocess, "run", run)

        directives._pkgutil(["org\\.example\\..*"])

        assert sorted(p.name for p in root.rglob("*")) == ["mine", "shared"]
        assert (tmp_path / "escape").exists()
        assert sudo_calls[-1] == ("/usr/sbin/pkgutil", "--forget", "org.example.foo")

    @pytest.mark.parametrize(("stderr", "warned"), [("", False), ("Error: bad", True)])
    def test_failed_listing_warns(
        self, monkeypatch, caplog, sudo_calls, stderr: str, warned: bool
    ) -> None:
        """Test that a listing error warns, but a bare exit 1 is just no match."""
        import logging

        monkeypatch.setattr(
            directives,
            "_run",
            lambda *a: subprocess.CompletedProcess(a, 1, "", stderr),
        )

        with caplog.at_level(logging.WARNING, logger=directives.__name__):
            directives._pkgutil(["["])

        assert any("Error: bad" in r.getMessage() for r in caplog.records) is warned
        assert sudo_calls == []

    def test_unreadable_package_needs_brew(self, monkeypatch, sudo_calls) -> None:
        """Test that a package pkgutil can't export raises before removing anything."""
        monkeypatch.setattr(
            directives.subprocess,
            "run",
            lambda cmd, **_: subprocess.CompletedProcess(cmd, 1, b"", b"no such pkg"),
        )

        with pytest.raises(CellarError, match="org.example.foo"):
            directives._remove_pkg("org.example.foo")

        assert sudo_calls == []


def test_delete_retries_what_the_user_cant_with_sudo(
    home, monkeypatch, sudo_calls
) -> None:
    """Test that permission failures are removed in one sudo call, as brew would."""
    for name in ("mine", "roots", "also"):
        (home / name).write_text("")

    def remove(path: Path) -> None:
        if path.name != "mine":
            raise PermissionError(13, "Permission denied")
        path.unlink()

    monkeypatch.setattr(directives, "remove", remove)

    directives._delete(["~/mine", "~/roots", "~/also"])

    assert not (home / "mine").exists()
    assert not (home / "roots").exists()
    assert not (home / "also").exists()
    assert sudo_calls == [("/usr/bin/xargs", "-0", "--", "/bin/rm", "-r", "-f", "--")]


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


class TestRunDirectives:
    """Tests for running directives in brew's order."""

    @pytest.mark.parametrize(
        ("upgrade", "on_upgrade", "signalled"),
        [(False, [], True), (True, [], False), (True, ["signal"], True)],
    )
    def test_upgrade_skips_signal_unless_on_upgrade(
        self, monkeypatch, upgrade, on_upgrade, signalled
    ) -> None:
        """Test brew's rule: an upgrade only signals what `on_upgrade` lists."""
        ran: list[str] = []
        monkeypatch.setitem(directives._RUNNERS, "signal", lambda _: ran.append("s"))
        monkeypatch.setitem(directives._RUNNERS, "quit", lambda ids: ids)
        stanza = {"quit": "a.b", "signal": ["TERM", "a.b"], "on_upgrade": on_upgrade}

        closed = directives.run_directives([stanza], deferred=False, upgrade=upgrade)

        assert closed == ["a.b"]
        assert bool(ran) is signalled

    def test_scripts_run_in_brews_order(self, tmp_path, monkeypatch) -> None:
        """Test that `early_script` runs first and `script` before `pkgutil`."""
        ran: list[str] = []
        monkeypatch.setattr(directives.time, "sleep", lambda _: None)
        monkeypatch.setattr(
            directives, "run_script", lambda spec, *_: ran.append(spec["executable"])
        )
        monkeypatch.setitem(directives._RUNNERS, "quit", lambda _: ran.append("quit"))
        monkeypatch.setitem(directives._RUNNERS, "pkgutil", lambda _: ran.append("pkg"))
        stanza = {
            "pkgutil": "a",
            "script": {"executable": "late"},
            "quit": "a.b",
            "early_script": {"executable": "early"},
        }

        directives.run_directives(
            [stanza], deferred=False, staged=tmp_path, prefix=tmp_path
        )

        assert ran == ["early", "quit", "late", "pkg"]


class TestRunScript:
    """Tests for running installer and uninstall scripts as brew does."""

    @pytest.fixture
    def script(self, tmp_path) -> Path:
        """A non-executable staged script recording its env, args and stdin.

        Args:
            tmp_path: The pytest tmp_path fixture.

        Returns:
            The script.
        """
        path = tmp_path / "bin" / "install.sh"
        path.parent.mkdir()
        path.write_text('#!/bin/sh\nenv > "$1"\ncat > "$2"\nexit "${3:-0}"\n')
        path.chmod(0o644)

        return path

    def test_runs_with_brews_env_args_and_input(
        self, tmp_path, script, monkeypatch
    ) -> None:
        """Test the filtered env and PATH, `+x`, args and `input` on stdin."""
        monkeypatch.setenv("VIRTUAL_ENV", "/venv")
        monkeypatch.setenv("HOMEBREW_FOO", "1")
        spec = {
            "executable": "bin/install.sh",
            "args": [str(tmp_path / "env"), str(tmp_path / "in")],
            "input": ["y\n", "n\n"],
        }

        directives.run_script(spec, tmp_path, Path("/opt/hb"))

        env = dict(
            line.split("=", 1) for line in (tmp_path / "env").read_text().splitlines()
        )
        assert env["PATH"] == "/opt/hb/bin:/opt/hb/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
        assert env["HOMEBREW_FOO"] == "1"
        assert "VIRTUAL_ENV" not in env
        assert (tmp_path / "in").read_text() == "y\nn\n"
        assert os.access(script, os.X_OK)

    def test_failure_raises_unless_it_need_not_succeed(self, tmp_path, script) -> None:
        """Test that a failing script raises with its code, or not if allowed to."""
        args = [str(tmp_path / "env"), str(tmp_path / "in"), "3"]
        spec = {"executable": str(script), "args": args}

        with pytest.raises(CellarError, match=r"failed \(3\)"):
            directives.run_script(spec, tmp_path, tmp_path)

        directives.run_script({**spec, "must_succeed": False}, tmp_path, tmp_path)

    def test_bare_name_is_looked_up_on_path(self, tmp_path) -> None:
        """Test that an executable not staged is found on the system PATH."""
        directives.run_script("true", tmp_path, tmp_path)

        with pytest.raises(CellarError, match="does not exist"):
            directives.run_script({"executable": "no/such.sh"}, tmp_path, tmp_path)

    def test_sudo_runs_through_run_sudo(self, tmp_path, script, monkeypatch) -> None:
        """Test that `sudo: true` hands the command, input and env to run_sudo."""
        calls = []

        def run_sudo(*args, stdin, env, check):
            calls.append((args, stdin, env["PATH"], check))
            return subprocess.CompletedProcess(args, 0, b"", b"")

        monkeypatch.setattr(directives, "run_sudo", run_sudo)

        directives.run_script(
            {
                "executable": "bin/install.sh",
                "args": ["-q"],
                "input": "y",
                "sudo": True,
            },
            tmp_path,
            Path("/opt/hb"),
        )

        path = "/opt/hb/bin:/opt/hb/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
        assert calls == [((str(script), "-q"), b"y", path, False)]


class TestLoginItem:
    """Tests for removing login items."""

    @staticmethod
    def _osascript(monkeypatch, code: int = 0) -> list[tuple[str, ...]]:
        """Fake `osascript`, exiting with `code`.

        Returns:
            The commands run.
        """
        calls: list[tuple[str, ...]] = []

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, code, "", "denied")

        monkeypatch.setattr(directives, "_run", run)
        return calls

    def test_removes_app_paths_then_names(self, monkeypatch) -> None:
        """Test that each app's path, then each name, is deleted, quoted for AppleScript."""
        calls = self._osascript(monkeypatch)

        directives.run_directives(
            [{"login_item": 'Foo "Helper"'}],
            deferred=False,
            apps=[Path("/Applications/Foo.app")],
        )

        tell = 'tell application "System Events" to delete every login item whose '
        assert calls == [
            ("osascript", "-e", tell + 'path is "/Applications/Foo.app"'),
            ("osascript", "-e", tell + 'name is "Foo \\"Helper\\""'),
        ]

    def test_upgrade_skips_login_items(self, monkeypatch) -> None:
        """Test that an upgrade leaves login items alone."""
        calls = self._osascript(monkeypatch)

        directives.run_directives(
            [{"login_item": "Foo"}],
            deferred=False,
            upgrade=True,
            apps=[Path("/Applications/Foo.app")],
        )

        assert calls == []

    def test_failure_only_warns(self, monkeypatch) -> None:
        """Test that a failed removal doesn't raise, so the uninstall carries on."""
        calls = self._osascript(monkeypatch, code=1)

        directives._login_item(["Foo", "Bar"], [])

        assert len(calls) == 2


class TestProcessCalls:
    """Tests that `quit` and `signal` don't re-run their lookups per bundle id."""

    @pytest.fixture(autouse=True)
    def _fresh_ancestors(self) -> Generator:
        """Forget the cached ancestry, which the faked `ps` stands in for."""
        directives._ancestors.cache_clear()
        yield
        directives._ancestors.cache_clear()

    @staticmethod
    def _fake(monkeypatch) -> list[tuple[str, ...]]:
        """Fake the commands for two running apps, `a.one` (pid 100) and `a.two` (200).

        Returns:
            The commands run.
        """
        calls: list[tuple[str, ...]] = []
        running = {"a.one": 100, "a.two": 200}
        uid = os.getuid()

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            out = ""
            if args[-1] == directives._RUNNING_APPS_JS:
                out = "\n".join(f"{pid}\t{bid}" for bid, pid in running.items())

            elif directives._QUIT_JS in args:
                running.pop(args[-1], None)

            elif args[0] == "who":
                out = f"{getpass.getuser()} console Jan  1 00:00"

            elif args[:2] == ("launchctl", "list"):
                out = "PID\tStatus\tLabel\n100\t0\ta.one\n200\t0\ta.two"

            elif args[:3] == ("ps", "-o", "pid=,uid="):
                out = "\n".join(f"{pid} {uid}" for pid in args[-1].split(","))

            return subprocess.CompletedProcess(args, 0, out, "")

        monkeypatch.setattr(directives, "_run", run)
        monkeypatch.setattr(directives.time, "sleep", lambda _: None)
        return calls

    def test_quit_lists_running_apps_and_checks_gui_once(self, monkeypatch) -> None:
        """Test that the running apps and `who` are read once, not per bundle id."""
        calls = self._fake(monkeypatch)

        assert directives._quit(["a.one", "a.two"]) == ["a.one", "a.two"]

        first_quit = next(i for i, c in enumerate(calls) if directives._QUIT_JS in c)
        before = calls[:first_quit]
        assert (
            before.count(
                ("osascript", "-l", "JavaScript", "-e", directives._RUNNING_APPS_JS)
            )
            == 1
        )
        assert [c for c in calls if c == ("who",)] == [("who",)]

    def test_signal_lists_launchd_jobs_once(self, monkeypatch) -> None:
        """Test that `launchctl list` runs once for every bundle id signalled."""
        calls = self._fake(monkeypatch)
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(
            directives.os, "kill", lambda pid, sig: killed.append((pid, sig))
        )

        directives._signal(["TERM", "a.one", "TERM", "a.two"])

        assert [c for c in calls if c[:2] == ("launchctl", "list")] == [
            ("launchctl", "list")
        ]
        assert killed == [(100, signal.SIGTERM), (200, signal.SIGTERM)]
