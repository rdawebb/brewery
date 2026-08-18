"""Shared stub classes for unit tests."""

from __future__ import annotations

from typing import Any, Self

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

    The pipeline module does a plain `import httpx`, so patching the attribute on
    the shared module object covers whichever entry point is under test.

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


class MockPorts:
    """The three ports the pipeline needs, as opaque sentinels.

    The pipeline only forwards them, so identity is all a test needs to assert;
    they are `Any` because no method on them is ever called.
    """

    def __init__(self) -> None:
        """Initialise with distinct catalog, cache_mgr, and formula sentinels."""
        self.catalog: Any = object()
        self.cache_mgr: Any = object()
        self.formula: Any = object()


async def _run_brew(args) -> None:
    """No-op brew runner stub used to construct a BrewAdapter in tests."""
    return
