"""User-authored preferences, persisted separately from the cache."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

import orjson

from brewery.core.config import get_config_dir
from brewery.core.errors import SettingsError, SysError
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


@dataclass(frozen=True)
class _Settable:
    """A single, settable value from the config file."""

    section: str
    name: str
    parse: Callable[[str], Any]


def _positive_int(raw: str) -> int:
    """Parse a positive integer from a string.

    Args:
        raw: The string to parse.

    Returns:
        The parsed integer.

    Raises:
        SettingsError: If the string cannot be parsed as a positive integer.
    """
    try:
        v = int(raw)

    except ValueError:
        raise SettingsError(f"'{raw}' is not an integer")

    if v <= 0:
        raise SettingsError("value must be greater than 0")

    return v


def _positive_int_or_none(raw: str) -> int | None:
    """Parse a positive integer from a string, or return None.

    Args:
        raw: The string to parse.

    Returns:
        The parsed integer, or None if the string is empty or represents a null value.
    """
    if raw.strip().lower() in {"null", "none", "unlimited", ""}:
        return None

    return _positive_int(raw)


def _choice(allowed: set[str]) -> Callable[[str], str]:
    """Return a parser function that validates a string against a set of allowed values.

    Args:
        allowed: The set of allowed values.

    Returns:
        A parser function that raises a SettingsError if the value is not in the allowed set.

    Raises:
        SettingsError: If the value is not in the allowed set.
    """

    def parse(raw: str) -> str:
        """Parse the raw string against the allowed set of values.

        Args:
            raw: The raw string to parse.

        Returns:
            The parsed string, or raises a SettingsError if the value is not in the allowed set.

        Raises:
            SettingsError: If the value is not in the allowed set.
        """
        v = raw.strip().lower()
        if v not in allowed:
            raise SettingsError(f"value must be one of: {', '.join(sorted(allowed))}")

        return v

    return parse


SETTABLE: dict[str, _Settable] = {
    "retention.age_days": _Settable("retention", "age_days", _positive_int),
    "retention.max_versions": _Settable(
        "retention", "max_versions", _positive_int_or_none
    ),
    "retention.max_cellar_mb": _Settable(
        "retention", "max_cellar_mb", _positive_int_or_none
    ),
    "daemon.cleanup_interval_days": _Settable(
        "daemon", "cleanup_interval_days", _positive_int
    ),
    "display.format": _Settable("display", "format", _choice({"rich", "plain"})),
}


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


def _load_raw() -> dict:
    """Parse the existing config strictly, refusing to proceed on corruption.

    Returns:
        The parsed config as a dict, or an empty dict if the file does not exist.

    Raises:
        SettingsError: The file is not valid JSON or not a JSON object.
        SysError: The existing file cannot be read (permissions, I/O).
    """
    path = _config_path()
    if not path.exists():
        return {}

    try:
        data = path.read_bytes()

    except OSError as e:
        raise SysError(f"cannot read config file {path} ({e})", context={"path": path})

    try:
        raw = orjson.loads(data)

    except orjson.JSONDecodeError as e:
        raise SettingsError(
            f"config file is not valid JSON ({e}); fix or delete it", path=path
        )

    if not isinstance(raw, dict):
        raise SettingsError(
            "config file is not a JSON object; fix or delete it", path=path
        )

    return raw


def _write_raw(raw: dict) -> None:
    """Write the raw config to the config file, preserving the file if possible.

    Args:
        raw: The config data to write.
    """
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(orjson.dumps(raw, option=orjson.OPT_INDENT_2))
    os.replace(tmp, path)


def write_setting(key: str, raw_value: str) -> Any:
    """Validate and persist one setting, preserving every other key.

    Args:
        key: Dotted key, e.g. 'retention.age_days'.
        raw_value: New value as a CLI string ('unlimited'/'null'/'' disables a cap).

    Returns:
        The parsed value written.

    Raises:
        SettingsError: Unknown key, invalid value, or an unreadable existing file.
    """
    field = SETTABLE.get(key)
    if field is None:
        raise SettingsError(
            f"unknown key '{key}'; valid: {', '.join(sorted(SETTABLE))}"
        )

    value = field.parse(raw_value)
    raw = _load_raw()  # Refuses to clobber a malformed file
    section = raw.get(field.section)
    raw[field.section] = section if isinstance(section, dict) else {}
    raw[field.section][field.name] = value
    _write_raw(raw)

    return value


def get_setting(key: str) -> Any:
    """Resolve one setting's effective value (file over defaults).

    Args:
        key: Dotted key, e.g. 'retention.age_days'.

    Returns:
        The setting's effective value.

    Raises:
        SettingsError: Unknown key.
    """
    field = SETTABLE.get(key)
    if field is None:
        raise SettingsError(
            f"unknown key '{key}'; valid: {', '.join(sorted(SETTABLE))}"
        )

    s = load_settings()

    return getattr(getattr(s, field.section), field.name)
