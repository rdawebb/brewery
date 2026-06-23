"""User-authored preferences, persisted separately from the cache."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import orjson

from brewery.core.config import get_config_dir
from brewery.core.logging import get_logger

log = get_logger(name=__name__)

CONFIG_NAME = "config.json"


@dataclass(frozen=True)
class RetentionSettings:
    """Cleanup retention policy. None disables a given limit."""

    age_days: int = 30
    max_versions: int | None = None
    max_cellar_mb: int | None = None


@dataclass(frozen=True)
class DaemonSettings:
    """Background daemon behaviour."""

    catalog_refresh_interval_mins: int = 30
    cleanup_interval_days: int = 1


@dataclass(frozen=True)
class DisplaySettings:
    """Output presentation."""

    format: str = "rich"  # "rich" | "plain"


@dataclass(frozen=True)
class Settings:
    """Top-level user settings; every field defaults, so a partial file is valid."""

    retention: RetentionSettings = field(default_factory=RetentionSettings)
    daemon: DaemonSettings = field(default_factory=DaemonSettings)
    display: DisplaySettings = field(default_factory=DisplaySettings)


def _coerce(cls, raw: Any):
    """Build a settings dataclass from a dict, ignoring unknown keys and bad types.

    A malformed section degrades to that section's defaults, unknown keys
    (forward-compat) and wrong-typed values are dropped with a warning.

    Args:
        cls: The settings dataclass to build.
        raw: The corresponding sub-dict from the config file.

    Returns:
        An instance of cls with valid provided values applied over defaults.
    """
    if not isinstance(raw, dict):
        return cls()

    known = {f.name: f.type for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in known:
            log.warning(event="settings_unknown_key", section=cls.__name__, key=key)
            continue

        kwargs[key] = value

    try:
        return cls(**kwargs)

    except TypeError as e:
        log.warning(
            event="settings_section_invalid", section=cls.__name__, error=str(e)
        )
        return cls()


def _config_path() -> Path:
    """Get the path to the config file, resolving the config directory first.

    Returns:
        The path to the config file.
    """
    return get_config_dir() / CONFIG_NAME


def load_settings() -> Settings:
    """Load settings from disk, falling back to defaults on absence or corruption.

    Returns:
        A Settings instance; never raises for a missing or malformed file.
    """
    try:
        raw = orjson.loads(_config_path().read_bytes())

    except FileNotFoundError:
        return Settings()

    except (OSError, orjson.JSONDecodeError) as e:
        log.warning(event="settings_unreadable", error=str(e))
        return Settings()

    if not isinstance(raw, dict):
        log.warning(event="settings_not_object")
        return Settings()

    return Settings(
        retention=_coerce(RetentionSettings, raw.get("retention", {})),
        daemon=_coerce(DaemonSettings, raw.get("daemon", {})),
        display=_coerce(DisplaySettings, raw.get("display", {})),
    )
