"""Unit tests for log-directory resolution and the logger wrapper."""

from __future__ import annotations

import logging
from pathlib import Path

import brewery.core.logging as logmod
from brewery.core.logging import BreweryLogger, _default_log_dir


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


class TestException:
    """Tests for the exception() helper on the logger wrapper."""

    def test_logs_at_error_with_traceback(self) -> None:
        """Test that exception() logs at ERROR and attaches the active traceback."""
        calls: list[tuple] = []

        class _Stub:
            def log(self, level, msg, *args, **kwargs) -> None:
                calls.append((level, msg % args, kwargs))

        log = BreweryLogger(_Stub())  # ty: ignore[invalid-argument-type]
        log.exception(event="boom", detail="context")

        level, message, kwargs = calls[0]
        assert level == logging.ERROR
        assert message == "boom | detail=context"
        assert kwargs["exc_info"] is True

    def test_explicit_exc_info_is_preserved(self) -> None:
        """Test that a caller-supplied exception overrides the default."""
        calls: list[tuple] = []

        class _Stub:
            def log(self, level, msg, *args, **kwargs) -> None:
                calls.append((level, msg % args, kwargs))

        error = ValueError("kaboom")
        log = BreweryLogger(_Stub())  # ty: ignore[invalid-argument-type]
        log.exception(event="boom", exc_info=error)

        assert calls[0][2]["exc_info"] is error
