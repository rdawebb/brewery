"""Symlink map caching functionality"""

from __future__ import annotations

import os
from pathlib import Path

from brewery.core.cache import Cache
from brewery.core.config import get_brewery_env

_SYMLINK_CACHE_KEY = "symlink_map"


def build_symlink_map(cache: Cache) -> dict[str, str]:
    """Return binary → formula name mapping, rebuilding only when cellar changes.

    Args:
        cache: The cache to use for storing the symlink map.

    Returns:
        A dictionary mapping binary names to their corresponding formula names.
    """
    cached = cache.get(_SYMLINK_CACHE_KEY)
    if cached is not None:
        return cached

    env = get_brewery_env()
    bin_path = env.prefix / "bin"
    result: dict[str, str] = {}

    if not bin_path.is_dir():
        cache.set(_SYMLINK_CACHE_KEY, result)

        return result

    for entry in os.scandir(bin_path):
        if entry.is_symlink():
            target = Path(os.readlink(entry.path))
            # Cellar symlinks are: prefix/Cellar/<formula>/<version>/bin/<binary>
            parts = target.parts
            if "Cellar" in parts:
                cellar_idx = parts.index("Cellar")
                if cellar_idx + 1 < len(parts):
                    result[entry.name] = parts[cellar_idx + 1]

    cache.set(_SYMLINK_CACHE_KEY, result)

    return result
