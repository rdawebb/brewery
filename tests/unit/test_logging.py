"""Unit tests for log-directory resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

import brewery.core.logging as logmod
from brewery.core.logging import _default_log_dir

pytestmark = pytest.mark.unit


class TestDefaultLogDir:
    """Tests for the platform-conventional default log directory."""

    def test_macos_uses_library_logs(self, monkeypatch) -> None:
        """Test that macOS resolves to ~/Library/Logs/brewery."""
        monkeypatch.setattr(logmod.platform, "system", lambda: "Darwin")
        assert _default_log_dir() == Path.home() / "Library" / "Logs" / "brewery"

    def test_linux_defaults_to_xdg_state_home_fallback(self, monkeypatch) -> None:
        """Test that Linux without XDG_STATE_HOME uses ~/.local/state."""
        monkeypatch.setattr(logmod.platform, "system", lambda: "Linux")
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert (
            _default_log_dir() == Path.home() / ".local" / "state" / "brewery" / "logs"
        )

    def test_linux_honours_xdg_state_home(self, monkeypatch) -> None:
        """Test that XDG_STATE_HOME redirects the Linux default log dir."""
        monkeypatch.setattr(logmod.platform, "system", lambda: "Linux")
        monkeypatch.setenv("XDG_STATE_HOME", "/custom/state")
        assert _default_log_dir() == Path("/custom/state") / "brewery" / "logs"
