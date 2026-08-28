"""Unit tests for settings validation, coercion and the settable-key registry."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import orjson
import pytest

from brewery.core.errors import SettingsError
from brewery.core.settings import (
    CONFIG_NAME,
    SETTABLE,
    DaemonSettings,
    DisplaySettings,
    RetentionSettings,
    Settings,
    _coerce,
    get_setting,
    load_settings,
    write_setting,
)


@pytest.fixture
def config_home(tmp_path, monkeypatch) -> Path:
    """Point the config directory at a temp dir for one test.

    Args:
        tmp_path: Pytest-provided temporary directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The temporary config directory.
    """
    monkeypatch.setenv("BREWERY_CONFIG_HOME", str(tmp_path))

    return tmp_path


def _write_config(config_home: Path, raw: dict) -> None:
    """Write a config file into the temp config directory.

    Args:
        config_home: The temporary config directory.
        raw: The config contents to serialise.
    """
    (config_home / CONFIG_NAME).write_bytes(orjson.dumps(raw))


class TestCoerce:
    """Tests for per-field validation when loading a section from disk."""

    def test_valid_values_applied_over_defaults(self) -> None:
        """Test that provided valid values win over the dataclass defaults."""
        s = _coerce(RetentionSettings, "retention", {"age_days": 7})
        assert s.age_days == 7

    def test_invalid_value_drops_only_that_field(self) -> None:
        """Test that one bad key does not reset its section's other values."""
        s = _coerce(
            RetentionSettings, "retention", {"age_days": "banana", "max_versions": 3}
        )

        assert s.age_days == RetentionSettings().age_days  # Fell back to the default
        assert s.max_versions == 3  # Sibling value survived

    @pytest.mark.parametrize("bad", ["thirty", -5, 0, True, None, 1.5, [1]])
    def test_rejects_non_positive_ints(self, bad) -> None:
        """Test that a required positive-int field rejects every wrong-typed value."""
        s = _coerce(RetentionSettings, "retention", {"age_days": bad})
        assert s.age_days == RetentionSettings().age_days

    def test_bool_is_not_an_integer(self) -> None:
        """Test that True is rejected rather than silently coerced to 1."""
        s = _coerce(DaemonSettings, "daemon", {"cleanup_interval_days": True})
        assert s.cleanup_interval_days is not True

    def test_none_allowed_for_optional_limits(self) -> None:
        """Test that None is accepted where it means 'no limit'."""
        s = _coerce(RetentionSettings, "retention", {"max_versions": None})
        assert s.max_versions is None

    def test_choice_field_rejects_unknown_value(self) -> None:
        """Test that display.format falls back when given an unsupported format."""
        s = _coerce(DisplaySettings, "display", {"format": "yaml"})
        assert s.format == DisplaySettings().format

    def test_choice_field_normalises_case(self) -> None:
        """Test that a valid choice is normalised the same way as on the CLI path."""
        s = _coerce(DisplaySettings, "display", {"format": " PLAIN "})
        assert s.format == "plain"

    def test_unknown_key_is_dropped(self) -> None:
        """Test that a forward-compat unknown key does not break the section."""
        s = _coerce(RetentionSettings, "retention", {"age_days": 7, "future": "x"})
        assert s.age_days == 7

    def test_non_dict_section_degrades_to_defaults(self) -> None:
        """Test that a scalar where a section was expected yields section defaults."""
        assert (
            _coerce(RetentionSettings, "retention", "nonsense") == RetentionSettings()
        )


class TestLoadSettings:
    """Tests for the end-to-end load path over a real config file."""

    def test_invalid_value_on_disk_does_not_reach_the_settings_object(
        self, config_home
    ) -> None:
        """Test that a hand-edited bad value is dropped rather than passed through."""
        _write_config(config_home, {"retention": {"age_days": "banana"}})

        assert load_settings().retention.age_days == RetentionSettings().age_days

    def test_negative_refresh_interval_never_reaches_the_daemon(
        self, config_home
    ) -> None:
        """Test that a negative interval cannot become a negative StartInterval."""
        _write_config(config_home, {"daemon": {"catalog_refresh_interval_mins": -5}})

        interval = load_settings().daemon.catalog_refresh_interval_mins
        assert interval == DaemonSettings().catalog_refresh_interval_mins
        assert interval > 0

    def test_missing_file_yields_defaults(self, config_home) -> None:
        """Test that an absent config file is not an error."""
        assert load_settings() == Settings()


class TestWriteSetting:
    """Tests for the CLI write path."""

    def test_refresh_interval_round_trips(self, config_home) -> None:
        """Test that the daemon's refresh interval is settable and readable back."""
        assert write_setting("daemon.catalog_refresh_interval_mins", "15") == 15
        assert get_setting("daemon.catalog_refresh_interval_mins") == 15

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", ""])
    def test_rejects_invalid_intervals(self, config_home, bad) -> None:
        """Test that an invalid interval is refused at write time."""
        with pytest.raises(SettingsError):
            write_setting("daemon.catalog_refresh_interval_mins", bad)

    def test_unknown_key_rejected(self, config_home) -> None:
        """Test that an unknown dotted key names the valid ones."""
        with pytest.raises(SettingsError, match="unknown key"):
            write_setting("daemon.nope", "1")

    def test_write_preserves_other_keys(self, config_home) -> None:
        """Test that writing one key does not clobber the rest of the file."""
        write_setting("retention.age_days", "7")
        write_setting("daemon.catalog_refresh_interval_mins", "15")

        s = load_settings()
        assert s.retention.age_days == 7
        assert s.daemon.catalog_refresh_interval_mins == 15


class TestSettableRegistry:
    """Guards against a settings field existing with no CLI surface."""

    def test_every_field_is_settable(self) -> None:
        """Test that each section field has a SETTABLE entry.

        `daemon.catalog_refresh_interval_mins` was read by the daemon while being
        unsettable and invisible in `config show`; this stops that recurring.
        """
        declared = {(s.section, s.name) for s in SETTABLE.values()}

        # Resolved off an instance: `from __future__ import annotations` makes
        # dataclasses' own `f.type` a string, not the section class
        defaults = Settings()
        expected = {
            (section.name, f.name)
            for section in fields(Settings)
            for f in fields(getattr(defaults, section.name))
        }

        assert expected == declared

    def test_refresh_interval_carries_a_restart_hint(self) -> None:
        """Test that changing the interval tells the user to restart the daemon."""
        hint = SETTABLE["daemon.catalog_refresh_interval_mins"].hint
        assert hint is not None and "daemon restart" in hint
