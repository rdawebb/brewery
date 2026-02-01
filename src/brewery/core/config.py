"""Configuration module for Brewery environment."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class BreweryENV:
    """Configuration for Brewery environment."""

    prefix: Path
    cellar: Path
    caskroom: Path


_DEF_CACHE = Path.home() / ".brewery" / "cache"
_DEF_CACHE.mkdir(parents=True, exist_ok=True)
_BREW_PREFIX_CACHE = _DEF_CACHE / "brew_prefix.txt"


def get_brewery_env() -> BreweryENV:
    """Get or discover Brewery environment based on system settings."""

    if _BREW_PREFIX_CACHE.exists():
        print(f"Brew prefix cache exists: {_BREW_PREFIX_CACHE}")
        try:
            prefix = Path(_BREW_PREFIX_CACHE.read_text().strip())
            print(f"Found brew prefix in cache: {prefix}")
        except Exception:
            prefix = None
    else:
        prefix = None

    if prefix is None:
        print("Attempting to discover brew prefix...")
        try:
            output = subprocess.check_output(["brew", "--prefix"], text=True).strip()
            prefix = Path(output)
            _BREW_PREFIX_CACHE.write_text(str(prefix))
            print(f"Cached brew prefix: {prefix}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            prefix = Path("/usr/local") / "brew"

    _brewery_env = BreweryENV(
        prefix=prefix, cellar=prefix / "Cellar", caskroom=prefix / "Caskroom"
    )
    print(f"Created BreweryENV: {_brewery_env}")

    return _brewery_env


CACHE_DIR = _DEF_CACHE
