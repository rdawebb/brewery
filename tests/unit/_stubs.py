"""Shared stub classes for unit tests."""

from __future__ import annotations

from typing import Self

import httpx


class MockClient:
    """Async context manager stub that records whether it was closed."""

    def __init__(self) -> None:
        """Initialise with no closed state and no recorded constructor kwargs."""
        self.closed = False
        self.kwargs: dict = {}

    async def __aenter__(self) -> Self:
        """Return self when entering the context.

        Returns:
            The mock client instance.
        """
        return self

    async def __aexit__(self, *exc) -> bool:
        """Set closed state to True when exiting the context.

        Returns:
            False to indicate no exception was handled.
        """
        self.closed = True
        return False


def patch_httpx(monkeypatch) -> MockClient:
    """Patch httpx.AsyncClient with a stub that records its constructor kwargs.

    Both service modules do a plain `import httpx`, so patching the attribute on
    the shared module object covers whichever service is under test.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The MockClient instance that the patched constructor returns.
    """
    client = MockClient()

    def _client(**kwargs) -> MockClient:
        """Record the constructor kwargs and return the stub.

        Args:
            **kwargs: The keyword arguments the service passes to httpx.

        Returns:
            The mock client instance.
        """
        client.kwargs = kwargs

        return client

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    return client


class MockRepo:
    """Minimal repo stub exposing catalog, cache_mgr, and formula attributes."""

    def __init__(self) -> None:
        """Initialise with mock catalog, cache_mgr, and formula objects."""
        self.catalog = object()
        self.cache_mgr = object()
        self.formula = object()


async def _run_brew(args) -> None:
    """No-op brew runner stub used to construct a BrewAdapter in tests."""
    return
