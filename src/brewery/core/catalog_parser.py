"""Catalog JSON parser and writer for formulae and casks."""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass
from typing import Any

import orjson

from brewery.core.catalog import Catalog
from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

# macOS major version -> Homebrew bottle codename
_MACOS_CODENAMES: dict[int, str] = {
    27: "golden_gate",
    26: "tahoe",
    15: "sequoia",
    14: "sonoma",
    13: "ventura",
    12: "monterey",
    11: "big_sur",
}

_MACOS_MAJORS_DESC: tuple[int, ...] = tuple(sorted(_MACOS_CODENAMES, reverse=True))

_ANY_TAG = "all"  # The :any / :any_skip_relocation bottle, arch-independent


@dataclass(frozen=True, slots=True)
class Bottle:
    """A resolved bottle for one platform."""

    url: str | None
    sha256: str | None
    cellar: str | None  # :any_skip_relocation | :any | <path>


@dataclass(frozen=True, slots=True)
class Platform:
    """The current build platform for bottle selection."""

    arch: str  # "arm64" | "x86_64"
    macos_major: int


def current_platform() -> Platform | None:
    """Detect the current macOS build platform, or None if not resolvable.

    Returns:
        The current platform, or None if not resolvable.
    """
    if _platform.system() != "Darwin":
        return None

    version: str = _platform.mac_ver()[0]
    if not version:
        return None

    try:
        major = int(version.split(".")[0])

    except ValueError:
        return None

    return Platform(arch=_platform.machine(), macos_major=major)


def _macos_tag(arch: str, codename: str) -> str:
    """Build a bottle tag for a macOS codename.

    Apple Silicon prefixes the arch, while Intel uses the bare codename.

    Args:
        arch: The CPU architecture.
        codename: The macOS codename.

    Returns:
        The bottle tag.
    """
    return f"arm64_{codename}" if arch == "arm64" else codename


def candidate_tags(platform: Platform) -> list[str]:
    """Bottle tags to try, in preference order, for a platform.

    Args:
        platform: The current platform.

    Returns:
        The list of candidate tags.
    """
    tags: list[str] = [
        _macos_tag(arch=platform.arch, codename=_MACOS_CODENAMES[major])
        for major in _MACOS_MAJORS_DESC
        if major <= platform.macos_major
    ]
    tags.append(_ANY_TAG)

    return tags


def platform_tag(platform: Platform) -> str:
    """The canonical exact tag for the current platform (stored in meta).

    Args:
        platform: The current platform.

    Returns:
        The canonical exact tag for the current platform.
    """
    codename: str = _MACOS_CODENAMES.get(
        platform.macos_major, str(platform.macos_major)
    )

    return _macos_tag(arch=platform.arch, codename=codename)


def resolve_bottle(files: dict[str, Any], platform: Platform | None) -> Bottle | None:
    """Resolve the best bottle for the platform from a `files` map.

    Args:
        files: `bottle.stable.files` keyed by tag.
        platform: The current platform, or None (source-only -> no bottle).

    Returns:
        The resolved Bottle, or None if no usable bottle exists.
    """
    if not files or platform is None:
        return None

    for tag in candidate_tags(platform=platform):
        entry = files.get(tag)
        if entry:
            return Bottle(
                url=entry.get("url"),
                sha256=entry.get("sha256"),
                cellar=entry.get("cellar"),
            )

    return None


def load_formulae(
    catalog: Catalog, body: bytes, platform: Platform | None = None
) -> int:
    """Parse a `formula.json` body and write the formula catalog.

    Args:
        catalog: The catalog store to write into.
        body: Raw (decoded) `formula.json` bytes.
        platform: Platform for bottle resolution; auto-detected if None.

    Returns:
        The number of formulae written.
    """
    resolved: Platform | None = platform or current_platform()
    entries: Any = orjson.loads(body)

    formulae: list[dict[str, Any]] = []
    deps: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []

    for obj in entries:
        row, obj_deps, obj_aliases = _parse_formula(obj=obj, platform=resolved)
        formulae.append(row)
        deps.extend(obj_deps)
        aliases.extend(obj_aliases)

    catalog.write_formulae(formulae=formulae, deps=deps, aliases=aliases)
    if resolved is not None:
        catalog.set_meta("platform_tag", platform_tag(platform=resolved))

    return len(formulae)


def load_casks(catalog: Catalog, body: bytes) -> int:
    """Parse a `cask.json` body and write the cask catalog.

    Args:
        catalog: The catalog store to write into.
        body: Raw (decoded) `cask.json` bytes.

    Returns:
        The number of casks written.
    """
    entries: Any = orjson.loads(body)
    casks: list[dict[str, Any]] = [_parse_cask(obj=obj) for obj in entries]
    catalog.write_casks(casks=casks)

    return len(casks)


def _parse_formula(
    obj: dict[str, Any], platform: Platform | None
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Trim one formula entry to its catalog row, deps, and alias rows.

    Args:
        obj: The formula object to parse.
        platform: The current platform, or None (source-only -> no bottle).

    Returns:
        A tuple of the catalog row, deps, and alias rows.
    """
    name: str = obj["name"]
    versions: dict[str, Any] = obj.get("versions") or {}
    bottle_stable: dict[str, Any] = (obj.get("bottle") or {}).get("stable") or {}
    bottle: Bottle | None = resolve_bottle(
        files=bottle_stable.get("files") or {}, platform=platform
    )

    row: dict[str, Any] = {
        "name": name,
        "desc": obj.get("desc"),
        "homepage": obj.get("homepage"),
        "tap": obj.get("tap"),
        "version": versions.get("stable") or "",
        "revision": obj.get("revision", 0),
        "version_scheme": obj.get("version_scheme", 0),
        "keg_only": bool(obj.get("keg_only", False)),
        "has_service": bool(obj.get("service")),
        "post_install": bool(obj.get("post_install_defined", False)),
        "bottle_url": bottle.url if bottle else None,
        "bottle_sha256": bottle.sha256 if bottle else None,
        "bottle_cellar": bottle.cellar if bottle else None,
        "bottle_rebuild": bottle_stable.get("rebuild", 0),
        "deprecated": bool(obj.get("deprecated", False)),
        "disabled": bool(obj.get("disabled", False)),
    }

    # Runtime dependencies only (build/optional could be added with other kinds)
    obj_deps: list[dict[str, Any]] = [
        {"pkg": name, "dep": dep, "kind": "runtime"}
        for dep in obj.get("dependencies") or []
    ]

    # Aliases and oldnames both resolve to the canonical name
    alias_names: list[str] = [
        *(obj.get("aliases") or []),
        *(obj.get("oldnames") or []),
    ]

    obj_aliases: list[dict[str, Any]] = [
        {"alias": alias, "name": name} for alias in alias_names
    ]

    return row, obj_deps, obj_aliases


def _parse_cask(obj: dict[str, Any]) -> dict[str, Any]:
    """Trim one cask entry to its catalog row.

    Args:
        obj: The cask object to parse.

    Returns:
        The parsed cask row.
    """
    names: Any = obj.get("name") or []
    display_name: str | None = names[0] if isinstance(names, list) and names else None

    return {
        "token": obj["token"],
        "name": display_name,
        "desc": obj.get("desc"),
        "homepage": obj.get("homepage"),
        "tap": obj.get("tap"),
        "version": obj.get("version"),
        "sha256": obj.get("sha256"),
        "url": obj.get("url"),
        "auto_updates": bool(obj.get("auto_updates", False)),
        "artifacts": _json_text(obj.get("artifacts")),
        "depends_on": _json_text(obj.get("depends_on")),
        "deprecated": bool(obj.get("deprecated", False)),
        "disabled": bool(obj.get("disabled", False)),
    }


def _json_text(value: Any) -> str | None:
    """Encode a JSON-able value to TEXT for storage, or None if empty.

    Args:
        value: The value to encode.

    Returns:
        The encoded value, or None if empty.
    """
    if not value:
        return None

    return orjson.dumps(value).decode()
