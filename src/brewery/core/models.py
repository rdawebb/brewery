"""Data models for Homebrew packages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
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

    @classmethod
    def from_dict(cls, data: dict) -> "Dependency":
        return cls(
            name=data["name"],
            optional=data.get("optional", False),
            build=data.get("build", False),
            test=data.get("test", False),
        )


def to_serializable(obj: Any) -> Any:
    """Convert an object to a serializable format.

    Args:
        obj: The object to convert.

    Returns:
        A serializable representation of the object.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_serializable(obj=item) for item in obj]
    if isinstance(obj, dict):
        return {key: to_serializable(obj=value) for key, value in obj.items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {key: to_serializable(obj=value) for key, value in asdict(obj).items()}

    return obj


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

    def to_serializable_dict(self) -> dict[str, Any]:
        """Convert the Package instance to a serializable dictionary."""
        return to_serializable(obj=self)

    @staticmethod
    def package_from_dict(data: dict[str, Any]) -> Package:
        """Create a Package instance from a dictionary."""
        return Package(
            name=data["name"],
            kind=PackageKind(value=data["kind"]),
            versions=data.get("versions", []),
            desc=data.get("desc"),
            status=PackageStatus(value=data.get("status", 0)),
            installed_on=(
                datetime.fromisoformat(data["installed_on"])
                if data.get("installed_on")
                else None
            ),
            size_kb=data.get("size_kb"),
            deps=[Dependency.from_dict(dep) for dep in data.get("deps", [])],
            used_by=data.get("used_by", []),
            tap=data.get("tap"),
            path=data.get("path"),
            metadata=data.get("metadata", {}),
        )
