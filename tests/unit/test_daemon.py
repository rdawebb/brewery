"""Unit tests for daemon launchd target/path helpers and plist patching."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from brewery.daemon import daemon as daemon_mod
from brewery.daemon.daemon import (
    PLIST_LABEL,
    _gui_domain,
    _patch_executable_paths,
    _service_target,
)

pytestmark = pytest.mark.unit


class TestTargets:
    """Tests for the launchd domain/target string builders."""

    def test_gui_domain(self, monkeypatch):
        """Test that the GUI domain is gui/<uid>."""
        monkeypatch.setattr(daemon_mod.os, "getuid", lambda: 501)
        assert _gui_domain() == "gui/501"

    def test_service_target(self, monkeypatch):
        """Test that the service target is <gui-domain>/<label>."""
        monkeypatch.setattr(daemon_mod.os, "getuid", lambda: 501)
        assert _service_target() == f"gui/501/{PLIST_LABEL}"


class TestPatchExecutablePaths:
    """Tests for _patch_executable_paths plist rewriting."""

    def _write_plist(self, path: Path) -> None:
        path.write_bytes(
            plistlib.dumps(
                {
                    "Label": PLIST_LABEL,
                    "ProgramArguments": ["/old/python", "-m", "x"],
                }
            )
        )

    def test_rewrites_interpreter_and_path(self, tmp_path, monkeypatch):
        """Test that arg[0] becomes the resolved python and PATH includes brew dir."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: {"python3": "/new/python3", "brew": "/opt/homebrew/bin/brew"}[
                name
            ],
        )
        _patch_executable_paths(plist)

        data = plistlib.loads(plist.read_bytes())
        assert data["ProgramArguments"][0] == "/new/python3"
        assert data["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin")

    def test_no_brew_leaves_plist_unchanged(self, tmp_path, monkeypatch):
        """Test that a missing brew aborts patching without modifying the plist."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        before = plist.read_bytes()
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: None if name == "brew" else "/new/python3",
        )
        _patch_executable_paths(plist)
        assert plist.read_bytes() == before

    def test_falls_back_to_sys_executable(self, tmp_path, monkeypatch):
        """Test that arg[0] uses sys.executable when python3 is not on PATH."""
        plist = tmp_path / "d.plist"
        self._write_plist(plist)
        monkeypatch.setattr(
            daemon_mod.shutil,
            "which",
            lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None,
        )
        monkeypatch.setattr(daemon_mod.sys, "executable", "/fallback/python")
        _patch_executable_paths(plist)

        data = plistlib.loads(plist.read_bytes())
        assert data["ProgramArguments"][0] == "/fallback/python"
