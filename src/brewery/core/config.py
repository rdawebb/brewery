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
        try:
            prefix = Path(_BREW_PREFIX_CACHE.read_text().strip())

        except Exception:
            prefix = None
    else:
        prefix = None

    if prefix is None:
        print("Attempting to discover brew prefix...")
        try:
            output: str = subprocess.check_output(
                args=["brew", "--prefix"], text=True
            ).strip()
            prefix = Path(output)
            _BREW_PREFIX_CACHE.write_text(data=str(object=prefix))
            print(f"Cached brew prefix: {prefix}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            prefix: Path = Path("/usr/local") / "brew"

    _brewery_env = BreweryENV(
        prefix=prefix, cellar=prefix / "Cellar", caskroom=prefix / "Caskroom"
    )

    return _brewery_env


CACHE_DIR: Path = _DEF_CACHE
