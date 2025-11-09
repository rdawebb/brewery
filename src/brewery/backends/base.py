"""Backend base classes and protocols for package management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

PkgType = Literal["formula", "cask"]


@dataclass
class Package:
    """Represents a Homebrew package."""

    name: str
    version: str
    desc: str | None
    installed_at: str | None
    size_human: str
    status: list[str]
    pkg_type: PkgType


class PackageBackend(Protocol):
    """Protocol for package backend implementations."""

    async def list_installed(self) -> list[Package]:
        """List all installed packages.

        Returns:
            list[Package]: A list of installed packages.
        """
        ...

    async def get_details(self, name: str) -> dict[str, Any]:
        """Get detailed information about a package.

        Args:
            name (str): The name of the package.

        Returns:
            dict[str, Any]: A dictionary containing package details.
        """
        ...