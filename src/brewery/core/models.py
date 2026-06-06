"""Data models for Homebrew packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, Flag, auto
from typing import Any


def effective_version(version: str, revision: int = 0) -> str:
    """Return the effective version string, including revision if non-zero.

    Args:
        version: The upstream version string.
        revision: The Homebrew package revision number (zero if absent)

    Returns:
        'version' if revision is zero, otherwise 'version.revision'
    """
    return f"{version}.{revision}" if revision > 0 else version


def split_keg_version(keg_name: str) -> tuple[str, int]:
    """Split an installed keg name into version and revision.

    Args:
        keg_name: Keg directory name

    Returns:
        A tuple of (version, revision)
    """
    head, sep, tail = keg_name.partition("_")
    if sep and tail.isdigit():
        return head, int(tail)

    return keg_name, 0


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


@dataclass(slots=True)
class InstalledRecord:
    """Filesystem-derived view of a single installed package."""

    name: str
    kind: PackageKind
    version: str
    revision: int = 0
    version_scheme: int | None = None
    installed_on: datetime | None = None
    # Receipt flags, captured now for a future `leaves`/autoremove
    installed_on_request: bool = False
    installed_as_dependency: bool = False
    deps: list[str] = field(default_factory=list)
    head: bool = False
    tap: str | None = None
    path: str | None = None  # Absolute path to the active keg/caskroom version
    stale_versions: list[str] = field(default_factory=list)
    linked: bool = False
    pinned: bool = False
    used_by: list[str] = field(default_factory=list)  # Installed reverse-deps
    size_kb: int | None = None  # Filled by attach_sizes()

    @staticmethod
    def _record_to_cache_dict(record: InstalledRecord) -> dict:
        """Serialise an InstalledRecord to a JSON-safe dict for the record cache.

        Args:
            record: The InstalledRecord to serialise.

        Returns:
            A JSON-safe dict representing the record.
        """
        return {
            "name": record.name,
            "kind": record.kind.value,
            "version": record.version,
            "revision": record.revision,
            "version_scheme": record.version_scheme,
            "installed_on": record.installed_on.isoformat()
            if record.installed_on
            else None,
            "installed_on_request": record.installed_on_request,
            "installed_as_dependency": record.installed_as_dependency,
            "deps": record.deps,
            "head": record.head,
            "tap": record.tap,
            "path": record.path,
            "stale_versions": record.stale_versions,
            "linked": record.linked,
            "pinned": record.pinned,
            "used_by": record.used_by,
            "size_kb": record.size_kb,
        }

    @staticmethod
    def _record_from_cache_dict(data: dict) -> InstalledRecord:
        """Rebuild an InstalledRecord from its cached dict.

        Args:
            data: The cached dict representing the record.

        Returns:
            The rebuilt InstalledRecord.
        """
        installed_on = data.get("installed_on")

        return InstalledRecord(
            name=data["name"],
            kind=PackageKind(data["kind"]),
            version=data["version"],
            revision=data.get("revision", 0),
            version_scheme=data.get("version_scheme"),
            installed_on=datetime.fromisoformat(installed_on) if installed_on else None,
            installed_on_request=data.get("installed_on_request", False),
            installed_as_dependency=data.get("installed_as_dependency", False),
            deps=data.get("deps", []),
            head=data.get("head", False),
            tap=data.get("tap"),
            path=data.get("path"),
            stale_versions=data.get("stale_versions", []),
            linked=data.get("linked", False),
            pinned=data.get("pinned", False),
            used_by=data.get("used_by", []),
            size_kb=data.get("size_kb"),
        )
