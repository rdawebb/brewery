"""Protocol definitions for package backends."""

from __future__ import annotations

from typing import List, Protocol

from brewery.core.models import Package


class PackageBackend(Protocol):
    """Protocol for package backends."""

    async def list_installed(self) -> List[Package]:
        """List installed packages."""
        ...

    async def info(self, name: str) -> Package:
        """Get package info by name."""
        ...
