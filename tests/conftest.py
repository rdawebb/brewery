"""Shared test configuration and fixtures for Brewery.

Redirects all on-disk state (cache, logs) into a temp dir so tests never touch
the real ~/.brewery directory, and resets the module-level singletons/caches
between tests so that test order cannot leak state.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolates on disk state at import time, before any brewery module is imported
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="brewery-tests-"))
os.environ["BREWERY_CACHE_DIR"] = str(_TMP_ROOT / "cache")
os.environ["BREWERY_LOG_DIR"] = str(_TMP_ROOT / "logs")


# Resets module-level state between tests to avoid state leakage (only already-imported modules)
_RESETTABLE: list[tuple[str, str, object]] = [
    ("brewery.core.config", "_env_cache", None),
    ("brewery.core.cache", "_cached_token", None),
    ("brewery.core.cache", "_token_timestamp", 0),
    ("brewery.providers.brew_cask", "_caskroom_path", None),
    # Lazily-created, event-loop-bound, cleared so it re-binds to each test's own loop
    ("brewery.providers.package_builder", "_SEMAPHORE", None),
    # Renderer width-cache load flag + dict.
    ("brewery.cli.renderers", "_width_cache_loaded", False),
]


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset known singletons/caches before and after each test.

    Yields to allow test execution, then resets state after.
    """

    def _apply() -> None:
        """Applies the reset by setting attributes to their initial values."""
        for modname, attr, value in _RESETTABLE:
            mod = sys.modules.get(modname)
            if mod is not None and hasattr(mod, attr):
                setattr(mod, attr, value)

        # Clear renderer width cache in place if present
        renderers = sys.modules.get("brewery.cli.renderers")
        if renderers is not None and hasattr(renderers, "_width_cache"):
            renderers._width_cache.clear()

        # Clear the on-disk file cache so persisted records cannot leak between tests
        import shutil

        cache_root = Path(os.environ["BREWERY_CACHE_DIR"])
        if cache_root.exists():
            shutil.rmtree(cache_root, ignore_errors=True)

    _apply()
    yield
    _apply()
