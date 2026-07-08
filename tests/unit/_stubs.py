"""Shared stub classes for unit tests."""

from __future__ import annotations


class MockClient:
    """Async context manager stub that records whether it was closed."""

    def __init__(self) -> None:
        """Initialise with no closed state."""
        self.closed = False

    async def __aenter__(self) -> MockClient:
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


class MockRepo:
    """Minimal repo stub exposing catalog, cache_mgr, and formula attributes."""

    def __init__(self) -> None:
        """Initialise with mock catalog, cache_mgr, and formula objects."""
        self.catalog = object()
        self.cache_mgr = object()
        self.formula = object()


async def _run_brew(args) -> None:
    """No-op brew runner stub used to construct a BrewAdapter in tests."""
    return None
