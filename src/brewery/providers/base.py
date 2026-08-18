"""Protocol definitions for package backends."""

from __future__ import annotations

from typing import Protocol


class InstallBackend(Protocol):
    """Protocol for backends that can install packages."""

    async def install(self, names: list[str]) -> list[str]:
        """Install package(s) by name."""
        ...


class UninstallBackend(Protocol):
    """Protocol for backends that can uninstall packages."""

    async def uninstall(self, names: list[str]) -> list[str]:
        """Uninstall package(s) by name."""
        ...


class UpgradeBackend(Protocol):
    """Protocol for backends that can upgrade packages."""

    async def upgrade(self, names: list[str]) -> list[str]:
        """Upgrade package(s) by name."""
        ...


class PackageBackend(InstallBackend, UninstallBackend, UpgradeBackend, Protocol):
    """Protocol for package backends."""
