"""Pipeline-only stubs; shared doubles live in tests/_mocks.py."""

from __future__ import annotations

from typing import Any


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
