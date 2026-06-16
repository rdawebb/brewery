"""Protocol definitions for package backends."""

from __future__ import annotations

from typing import Protocol


class PackageBackend(Protocol):
    """Protocol for package backends."""

    async def install(self, names: list[str]) -> list[str]:
        """Install package(s) by name."""
        ...

    async def uninstall(self, names: list[str]) -> list[str]:
        """Uninstall package(s) by name."""
        ...

    async def upgrade(self, names: list[str]) -> list[str]:
        """Upgrade package(s) by name."""
        ...
