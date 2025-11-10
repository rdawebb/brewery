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

def discover_env() -> BreweryENV:
    """Discover Brewery environment based on system settings."""
    try:
        output = subprocess.check_output(["brew", "--prefix"], text=True).strip()
        prefix = Path(output)
    except (subprocess.CalledProcessError, FileNotFoundError):
        prefix = Path("/usr/local") / "brew"
    
    cellar = prefix / "Cellar"
    caskroom = prefix / "Caskroom"

    return BreweryENV(prefix=prefix, cellar=cellar, caskroom=caskroom)

Brewery = discover_env()
CACHE_DIR = _DEF_CACHE