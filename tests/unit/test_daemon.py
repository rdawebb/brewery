"""Unit tests for daemon launchd target/path helpers and plist patching."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from brewery.core.errors import SysError, UserError
from brewery.core.settings import load_settings, write_setting
from brewery.daemon import launchd as daemon_mod
from brewery.daemon.launchd import (
    PLIST_LABEL,
    _gui_domain,
    _service_target,
    patch_plist,
)

pytestmark = pytest.mark.unit


class TestTargets:
    """Tests for the launchd domain/target string builders."""

    def test_gui_domain(self, monkeypatch) -> None:
        """Test that the GUI domain is gui/<uid>."""
        monkeypatch.setattr(daemon_mod.os, "getuid", lambda: 501)
        assert _gui_domain() == "gui/501"

    def test_service_target(self, monkeypatch) -> None:
        """Test that the service target is <gui-domain>/<label>."""
        monkeypatch.setattr(daemon_mod.os, "getuid", lambda: 501)
        assert _service_target() == f"gui/501/{PLIST_LABEL}"


class TestPatchExecutablePaths:
    """Tests for patch_plist plist rewriting."""

    def _write_plist(self, path: Path) -> None:
        """Write a sample plist file for testing.

        Args:
            path: The path to the plist file to create.
        """
        path.write_bytes(
            plistlib.dumps(
                {
                    "Label": PLIST_LABEL,
                    "ProgramArguments": ["/old/python", "-m", "x"],
                }
            )
        )

    def test_rewrites_interpreter_and_path(self, tmp_path, monkeypatch) -> None:
        """Test that arg[0] becomes the resolved python and PATH includes brew dir."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(daemon_mod.sys, "executable", "/venv/bin/python3")
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: {
                "python3": "/usr/bin/python3",
                "brew": "/opt/homebrew/bin/brew",
            }[name],
        )
        patch_plist(plist)

        data = plistlib.loads(plist.read_bytes())
        # sys.executable must win: the system python3 cannot import brewery
        assert data["ProgramArguments"][0] == "/venv/bin/python3"
        assert data["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin")

    def test_falls_back_to_path_python_without_sys_executable(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test that a missing sys.executable falls back to python3 on PATH."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(daemon_mod.sys, "executable", "")
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: {
                "python3": "/usr/bin/python3",
                "brew": "/opt/homebrew/bin/brew",
            }[name],
        )
        patch_plist(plist)

        data = plistlib.loads(plist.read_bytes())
        assert data["ProgramArguments"][0] == "/usr/bin/python3"

    def test_writes_start_interval_not_refresh_interval(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test that the patched plist uses launchd's StartInterval key, in seconds."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: {
                "python3": "/usr/bin/python3",
                "brew": "/opt/homebrew/bin/brew",
            }[name],
        )
        monkeypatch.setenv("BREWERY_CONFIG_HOME", str(tmp_path / "config"))
        write_setting("daemon.catalog_refresh_interval_mins", "15")
        patch_plist(plist)

        data = plistlib.loads(plist.read_bytes())
        assert data["StartInterval"] == 15 * 60
        assert data["StartInterval"] == (
            load_settings().daemon.catalog_refresh_interval_mins * 60
        )

    def test_writes_log_paths_under_the_log_dir(self, tmp_path, monkeypatch) -> None:
        """Test that launchd's stderr/stdout go to the log dir, never /tmp."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        log_dir = tmp_path / "logs"
        monkeypatch.setenv("BREWERY_LOG_DIR", str(log_dir))
        monkeypatch.setattr(
            daemon_mod.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}"
        )
        patch_plist(plist)

        data = plistlib.loads(plist.read_bytes())
        assert data["StandardErrorPath"] == str(log_dir / "refresh.err")
        assert data["StandardOutPath"] == str(log_dir / "refresh.out")
        assert log_dir.is_dir()  # launchd will not create it

    def test_no_brew_still_patches_the_rest(self, tmp_path, monkeypatch) -> None:
        """Test that a missing brew only costs the PATH entry, not the whole patch."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(daemon_mod.sys, "executable", "/venv/bin/python3")
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: None if name == "brew" else "/new/python3",
        )

        warnings = patch_plist(plist)

        data = plistlib.loads(plist.read_bytes())
        assert len(warnings) == 1 and "brew" in warnings[0]
        assert data["ProgramArguments"][0] == "/venv/bin/python3"
        assert "StartInterval" in data
        assert "StandardErrorPath" in data
        assert "EnvironmentVariables" not in data  # PATH is the only casualty

    def test_no_interpreter_leaves_argv_alone(self, tmp_path, monkeypatch) -> None:
        """Test that an unresolvable interpreter advises rather than raising."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(daemon_mod.sys, "executable", "")
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
        )

        warnings = patch_plist(plist)

        data = plistlib.loads(plist.read_bytes())
        assert len(warnings) == 1 and "python" in warnings[0]
        assert data["ProgramArguments"][0] == "/old/python"  # Untouched, not None

    def test_falls_back_to_sys_executable(self, tmp_path, monkeypatch) -> None:
        """Test that arg[0] uses sys.executable when python3 is not on PATH."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
        )
        monkeypatch.setattr(daemon_mod.sys, "executable", "/fallback/python")
        patch_plist(plist)

        data = plistlib.loads(plist.read_bytes())
        assert data["ProgramArguments"][0] == "/fallback/python"

    def test_no_brew_returns_an_advisory_rather_than_printing(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """Test that the launchd layer reports advisories instead of writing to stdout."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: None if name == "brew" else "/new/python3",
        )

        warnings = patch_plist(plist)

        assert len(warnings) == 1
        assert "brew" in warnings[0]
        assert capsys.readouterr().out == ""

    def test_successful_patch_returns_no_advisories(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test that a fully patched plist yields no advisories."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: {
                "python3": "/usr/bin/python3",
                "brew": "/opt/homebrew/bin/brew",
            }[name],
        )

        assert patch_plist(plist) == []


class TestServiceControl:
    """The launchd layer raises instead of calling sys.exit, and never prints."""

    def test_stop_raises_when_not_installed(self, tmp_path, monkeypatch) -> None:
        """Test that stopping an uninstalled daemon is a UserError (exit 1 at the CLI)."""
        monkeypatch.setattr(daemon_mod, "PLIST_DEST", tmp_path / "absent.plist")

        with pytest.raises(UserError, match="not installed"):
            daemon_mod.stop()

    def test_start_raises_when_bootstrap_fails(self, tmp_path, monkeypatch) -> None:
        """Test that a non-zero `launchctl bootstrap` surfaces as a SysError."""
        monkeypatch.setattr(daemon_mod, "PLIST_DEST", tmp_path / "d.plist")
        monkeypatch.setattr(daemon_mod, "LAUNCH_AGENTS", tmp_path)
        monkeypatch.setattr(daemon_mod, "is_running", lambda: False)

        source = tmp_path / "src.plist"
        source.write_bytes(plistlib.dumps({"Label": PLIST_LABEL}))
        monkeypatch.setattr(daemon_mod, "_plist_source", lambda: source)
        monkeypatch.setattr(daemon_mod, "patch_plist", lambda _: [])
        monkeypatch.setattr(
            daemon_mod.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=5),
        )

        with pytest.raises(SysError, match="bootstrap failed") as exc:
            daemon_mod.start()

        assert exc.value.context["returncode"] == 5

    def test_is_running_reflects_launchctl_returncode(self, monkeypatch) -> None:
        """Test that is_running is a pure predicate over `launchctl print`."""
        monkeypatch.setattr(
            daemon_mod.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=0),
        )
        assert daemon_mod.is_running() is True

        monkeypatch.setattr(
            daemon_mod.subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], returncode=1),
        )
        assert daemon_mod.is_running() is False
