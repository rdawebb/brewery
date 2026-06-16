"""Shared test configuration and fixtures for Brewery.

Redirects all on-disk state (cache, logs) into a temp dir so tests never touch
the real ~/.brewery directory, and resets the module-level singletons/caches
between tests so that test order cannot leak state.
"""

from __future__ import annotations
from typing import Generator

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
def _reset_module_state() -> Generator[None, None, None]:
    """Reset known singletons/caches before each test."""
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

    yield


class MockHTTPClient:
    """Async httpx-like stub shared by the catalog fetch/refresh tests.

    Construct with either a single canned response/exception, or a mapping of
    ``url -> response``. Every GET is recorded (url + request headers) so tests
    can assert that conditional validators were sent, and ``aclose()`` flips
    ``closed`` so client-ownership tests can check the caller did not close an
    injected client.

    Args:
        response: One of an ``httpx.Response``, an ``Exception`` to raise, a
            ``dict[str, httpx.Response]`` keyed by URL, or ``None``.
        raise_on_get: If set, every GET raises this exception (used for
            transport-error paths), regardless of ``response``.
    """

    def __init__(self, response=None, *, raise_on_get=None) -> None:
        """Initialise a MockHTTPClient.

        Args:
            response: One of an `httpx.Response`, an `Exception` to raise, a
                `dict[str, httpx.Response]` keyed by URL, or `None`.
            raise_on_get: If set, every GET raises this exception (used for
                transport-error paths), regardless of `response`.
        """
        self._map = response if isinstance(response, dict) else None
        self._single = None if isinstance(response, dict) else response
        self._raise_on_get = raise_on_get
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        follow_redirects: bool = False,
    ) -> None:
        """
        Simulate an HTTP GET request.

        Args:
            url: The URL to fetch.
            headers: Headers to include in the request.
            timeout: Request timeout.
            follow_redirects: Whether to follow redirects.
        """
        self.last_url = url
        self.last_headers = dict(headers or {})
        self.requests.append((url, dict(headers or {})))

        if self._raise_on_get is not None:
            raise self._raise_on_get

        if self._map is not None:
            if url not in self._map:
                raise AssertionError(f"unexpected URL fetched: {url}")

            return self._map[url]

        if isinstance(self._single, Exception):
            raise self._single

        return self._single

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def mock_env(tmp_path, monkeypatch):
    """A hermetic BreweryENV backed by tmp_path with no real filesystem layout.

    Patches the module-level ``_env_cache`` singleton so any code path that
    calls ``get_brewery_env()`` without an explicit ``env=`` argument gets this
    instance.  Integration tests override this fixture in their own conftest to
    add a pre-populated keg layout.
    """
    from brewery.core import config
    from brewery.core.config import BreweryENV

    prefix = tmp_path / "homebrew"
    cache = tmp_path / "cache"
    env = BreweryENV(
        prefix=prefix,
        cellar=prefix / "Cellar",
        caskroom=prefix / "Caskroom",
        repository=prefix / "Library" / "Homebrew",
        api_path=cache / "api" / "formula.jws.json",
        bottle_cache=cache,
    )
    monkeypatch.setattr(config, "_env_cache", env)
    return env


@pytest.fixture
def http_client():
    """Factory for a MockHTTPClient.

    Returns a callable so each test builds its own client with the response
    shape it needs.
    """

    def _make(response=None, *, raise_on_get=None) -> MockHTTPClient:
        return MockHTTPClient(response, raise_on_get=raise_on_get)

    return _make
