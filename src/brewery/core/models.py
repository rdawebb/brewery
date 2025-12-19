"""Data models for Homebrew packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, Flag, auto
from typing import Any


class PackageKind(Enum):
    """Enumeration of package kinds."""

    FORMULA = "formula"
    CASK = "cask"


class PackageStatus(Flag):
    """Enumeration of package statuses."""

    NONE = 0
    OUTDATED = auto()
    PINNED = auto()
    NOT_LINKED = auto()
    KEG_ONLY = auto()
    HEAD = auto()
    HAS_SERVICE = auto()


@dataclass
class Dependency:
    """Represents a package dependency."""

    name: str
    optional: bool = False
    build: bool = False
    test: bool = False


@dataclass
class Package:
    """Represents a Homebrew package."""

    name: str
    kind: PackageKind
    versions: list[str] = field(default_factory=list)
    desc: str | None = None
    status: PackageStatus = PackageStatus.NONE
    installed_on: datetime | None = None
    size_kb: int | None = None
    deps: list[Dependency] = field(default_factory=list)
    used_by: list[str] = field(default_factory=list)
    tap: str | None = None
    path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
