"""Core models for package management system."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

PkgType = Literal["formula", "cask"]


@dataclass(frozen=True)
class PackageRow:
    """Represents a package row in the package table."""
    
    key: str
    name: str
    type: PkgType
    version: str
    status: str
    size_human: str
    installed_at: Optional[str] = None