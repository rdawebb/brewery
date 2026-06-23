"""Configuration module for Brewery environment."""

from __future__ import annotations

import os
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
    repository: Path
    api_path: Path
    bottle_cache: Path
    cache: Path


_DEF_CACHE = Path(
    os.environ.get(key="BREWERY_CACHE_DIR", default=Path.home() / ".brewery" / "cache")
)

DEFAULT_CACHE = (
    Path.home() / "Library" / "Caches" / "Homebrew"
    if platform.system() == "Darwin"
    else Path.home() / ".cache" / "Homebrew"
)
HOMEBREW_CACHE = Path(os.environ.get("HOMEBREW_CACHE", str(DEFAULT_CACHE)))
FORMULA_API_PATH = HOMEBREW_CACHE / "api" / "formula.jws.json"

_env_cache: BreweryENV | None = None


def ensure_cache_dir() -> Path:
    """Ensure the cache directory exists.

    Returns:
        The cache directory path.
    """
    _DEF_CACHE.mkdir(parents=True, exist_ok=True)

    return _DEF_CACHE


def _resolve_brew_path(flag: str, cache_file: Path, fallback: Path) -> Path:
    """Resolve the Homebrew path for a given flag.

    Args:
        flag: The Homebrew command flag.
        cache_file: The path to the cache file.
        fallback: The fallback path to use if resolution fails.

    Returns:
        The resolved Homebrew path.
    """
    if cache_file.exists():
        try:
            return Path(cache_file.read_text().strip())

        except Exception:
            log.warning(event=f"brew_{flag.lstrip('-')}_cache_read_failure")

    log.info(event=f"brew_{flag.lstrip('-')}_discover_start")
    try:
        value = Path(subprocess.check_output(args=["brew", flag], text=True).strip())
        ensure_cache_dir()
        cache_file.write_text(data=str(object=value))
        log.info(event=f"brew_{flag.lstrip('-')}_cached", path=str(object=value))

        return value

    except (subprocess.CalledProcessError, FileNotFoundError):
        return fallback


def get_brewery_env() -> BreweryENV:
    """Get or discover Brewery environment based on system settings.

    Returns:
        The Brewery environment.
    """
    global _env_cache

    if _env_cache is not None:
        return _env_cache

    _is_arm = platform.machine() == "arm64"
    _FALLBACK_PREFIX = Path("/opt/homebrew") if _is_arm else Path("/usr/local")

    prefix: Path = _resolve_brew_path(
        flag="--prefix",
        cache_file=_DEF_CACHE / "brew_prefix.txt",
        fallback=_FALLBACK_PREFIX,
    )
    repository: Path = _resolve_brew_path(
        flag="--repository",
        cache_file=_DEF_CACHE / "brew_repository.txt",
        fallback=prefix if _is_arm else prefix / "Homebrew",
    )

    bottle_cache: Path = HOMEBREW_CACHE
    api_path: Path = FORMULA_API_PATH

    _env_cache = BreweryENV(
        prefix=prefix,
        cellar=prefix / "Cellar",
        caskroom=prefix / "Caskroom",
        repository=repository,
        api_path=api_path,
        bottle_cache=bottle_cache,
        cache=ensure_cache_dir(),
    )

    return _env_cache


def get_config_dir() -> Path:
    """Resolve the brewery config directory (XDG, overridable for tests).

    Returns:
        $BREWERY_CONFIG_HOME, else $XDG_CONFIG_HOME/brewery, else ~/.config/brewery.
    """
    override = os.environ.get("BREWERY_CONFIG_HOME")
    if override:
        return Path(override)

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"

    return base / "brewery"
