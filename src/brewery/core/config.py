"""Configuration module for Brewery environment."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(__name__)


@dataclass
class BreweryENV:
    """Configuration for Brewery environment."""

    prefix: Path
    cellar: Path
    caskroom: Path


_DEF_CACHE = Path.home() / ".brewery" / "cache"
_DEF_CACHE.mkdir(parents=True, exist_ok=True)
_BREW_PREFIX_CACHE = _DEF_CACHE / "brew_prefix.txt"
_FALLBACK_PREFIX = (
    Path("/opt/homebrew") if platform.machine() == "arm64" else Path("/usr/local")
)

_env_cache: BreweryENV | None = None


def get_brewery_env() -> BreweryENV:
    """Get or discover Brewery environment based on system settings."""
    global _env_cache

    if _env_cache is not None:
        return _env_cache

    if _BREW_PREFIX_CACHE.exists():
        try:
            prefix = Path(_BREW_PREFIX_CACHE.read_text().strip())

        except Exception:
            prefix = None

    else:
        prefix = None

    if prefix is None:
        log.info(event="brew_prefix_discover_start")
        try:
            output: str = subprocess.check_output(
                args=["brew", "--prefix"], text=True
            ).strip()
            prefix = Path(output)
            _BREW_PREFIX_CACHE.write_text(data=str(object=prefix))
            log.info(event="brew_prefix_cached", prefix=str(prefix))

        except (subprocess.CalledProcessError, FileNotFoundError):
            prefix = _FALLBACK_PREFIX

    _env_cache = BreweryENV(
        prefix=prefix, cellar=prefix / "Cellar", caskroom=prefix / "Caskroom"
    )

    return _env_cache


CACHE_DIR: Path = _DEF_CACHE

KNOWN_COMMANDS: set[str] = {
    # List commands/aliases
    "list",
    "ls",
    "l",
    # Info commands/aliases
    "info",
    "i",
    "in",
    # Search commands/aliases
    "search",
    "s",
    "find",
    # Install commands/aliases
    "install",
    "add",
    # Uninstall commands/aliases
    "uninstall",
    "rm",
    "remove",
    # Outdated commands/aliases
    "outdated",
    "o",
    "out",
    # Upgrade commands/aliases
    "upgrade",
    "u",
    "up",
    # Daemon commands/aliases
    "daemon",
}

DAEMON_SUBCOMMANDS: set[str] = {
    "start",
    "a",
    "stop",
    "d",
    "status",
    "st",
    "stat",
}
