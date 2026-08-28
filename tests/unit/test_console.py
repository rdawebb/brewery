"""Unit tests for the CLI console built from `display.format`."""

from __future__ import annotations

from brewery.cli.context import _make_console
from brewery.core import settings as settings_mod
from brewery.core.settings import DisplaySettings, Settings


def _with_format(monkeypatch, fmt: str) -> None:
    """Make `load_settings` report a given display format.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        fmt: The display format to report ("rich" or "plain").
    """
    monkeypatch.setattr(
        settings_mod,
        "load_settings",
        lambda: Settings(display=DisplaySettings(format=fmt)),
    )


class TestMakeConsole:
    """Tests for `cli.context._make_console`."""

    def test_rich_leaves_terminal_detection_alone(self, monkeypatch) -> None:
        """Test that the default format does not force the terminal either way."""
        _with_format(monkeypatch, "rich")
        console = _make_console()

        assert console._force_terminal is None
        assert console.no_color is False

    def test_plain_renders_as_though_piped(self, monkeypatch) -> None:
        """Test that "plain" drops colour and reports a non-terminal."""
        _with_format(monkeypatch, "plain")
        console = _make_console()

        assert console.is_terminal is False
        assert console.no_color is True

    def test_emoji_and_highlight_stay_off_in_both(self, monkeypatch) -> None:
        """Test that the settings-independent console options are unchanged."""
        for fmt in ("rich", "plain"):
            _with_format(monkeypatch, fmt)
            console = _make_console()

            assert console._emoji is False
            assert console._highlight is False
