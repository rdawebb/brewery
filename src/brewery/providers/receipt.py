"""Write a brew-compatible `INSTALL_RECEIPT.json` (the keg "tab")."""

from __future__ import annotations

import os
import platform
import tempfile
from dataclasses import dataclass
from pathlib import Path

import orjson

RECEIPT_NAME = "INSTALL_RECEIPT.json"


@dataclass(frozen=True)
class RuntimeDependency:
    """Class representing a single runtime dependency"""

    full_name: str
    version: str
    revision: int = 0
    bottle_rebuild: int = 0
    pkg_version: str | None = None  # Defaults to `version`
    declared_directly: bool = False

    @classmethod
    def from_tab(cls, dep: dict) -> "RuntimeDependency":
        """Build from a tab runtime_dependency, ignoring compatibility_version.

        Args:
            dep: A runtime dependency dict from the bottle tab manifest.

        Returns:
            A RuntimeDependency populated from the tab fields.
        """
        return cls(
            full_name=dep["full_name"],
            version=dep["version"],
            revision=dep.get("revision", 0),
            bottle_rebuild=dep.get("bottle_rebuild", 0),
            pkg_version=dep.get("pkg_version"),
            declared_directly=dep.get("declared_directly", False),
        )

    def to_dict(self) -> dict:
        """Serialise to a brew-compatible runtime dependency dict.

        Returns:
            A dict matching the structure brew writes in INSTALL_RECEIPT.json.
        """
        return {
            "full_name": self.full_name,
            "version": self.version,
            "revision": self.revision,
            "bottle_rebuild": self.bottle_rebuild,
            "pkg_version": self.pkg_version
            if self.pkg_version is not None
            else self.version,
            "declared_directly": self.declared_directly,
        }


@dataclass(frozen=True)
class Source:
    stable_version: str
    api_path: str
    version_scheme: int = 0
    tap: str = "homebrew/core"
    spec: str = "stable"
    head: str | None = None
    compatibility_version: str | None = None
    tap_git_head: str | None = None

    def to_dict(self) -> dict:
        """Serialise to a brew-compatible source dict.

        Returns:
            A dict matching the `source` field brew writes in INSTALL_RECEIPT.json.
        """
        return {
            "spec": self.spec,
            "versions": {
                "stable": self.stable_version,
                "head": self.head,
                "version_scheme": self.version_scheme,
                "compatibility_version": self.compatibility_version,
            },
            "path": self.api_path,
            "tap_git_head": self.tap_git_head,
            "tap": self.tap,
        }


def build_receipt(
    *,
    homebrew_version: str,
    changed_files: list[str],
    source_modified_time: int,
    compiler: str,
    runtime_dependencies: list[RuntimeDependency],
    built_on: dict | None,
    installed_on_request: bool,
    time: int,
    source: Source,
    aliases: list[str],
    used_options: list[str] = [],
    unused_options: list[str] = [],
    built_as_bottle: bool = True,
    poured_from_bottle: bool = True,
    loaded_from_api: bool = True,
    loaded_from_internal_api: bool = False,
) -> dict:
    """Assemble the receipt dict in brew's exact field order.

    `arch` falls back to the host arch when the tab omits it (all bottles);
    `built_on` is written verbatim, or null when the tab omits it.

    Args:
        homebrew_version: The Homebrew version string from the bottle tab.
        changed_files: Files modified during the build, from the bottle tab.
        source_modified_time: Unix timestamp of the formula source, from the tab.
        compiler: The compiler used to build the bottle, from the tab.
        runtime_dependencies: Runtime deps resolved from the catalog.
        built_on: Platform dict from the tab, or None if absent.
        arch: CPU architecture of the host machine.
        installed_on_request: True if the formula was explicitly requested.
        time: Unix timestamp of this installation.
        source: Formula source metadata from the catalog.
        aliases: Alias names that resolve to this formula.
        used_options: Build options used (empty for bottle installs).
        unused_options: Build options not used (empty for bottle installs).
        built_as_bottle: Whether the keg was built as a bottle.
        poured_from_bottle: Whether the keg was poured from a bottle.
        loaded_from_api: Whether the formula was loaded from the JSON API.
        loaded_from_internal_api: Whether loaded from the internal API.

    Returns:
        A dict matching the exact structure brew writes for INSTALL_RECEIPT.json.
    """
    return {
        "homebrew_version": homebrew_version,
        "used_options": list(used_options),
        "unused_options": list(unused_options),
        "built_as_bottle": built_as_bottle,
        "poured_from_bottle": poured_from_bottle,
        "loaded_from_api": loaded_from_api,
        "loaded_from_internal_api": loaded_from_internal_api,
        "installed_on_request": installed_on_request,
        "changed_files": sorted(changed_files),
        "time": time,
        "source_modified_time": source_modified_time,
        "compiler": compiler,
        "aliases": list(aliases),
        "runtime_dependencies": [d.to_dict() for d in runtime_dependencies],
        "source": source.to_dict(),
        "arch": current_arch(),
        "built_on": built_on,
    }


def dumps_bytes(receipt: dict) -> bytes:
    """Serialise exactly as brew does: 2-space indent, key order preserved,
    no trailing newline (Ruby JSON.pretty_generate).

    Args:
        receipt: The receipt dict produced by build_receipt().

    Returns:
        UTF-8 JSON bytes matching brew's output format.
    """
    return orjson.dumps(receipt, option=orjson.OPT_INDENT_2)


def dumps(receipt: dict) -> str:
    """Serialise a receipt to a UTF-8 JSON string matching brew's output.

    The string form of `dumps_bytes` for brew fidelity testing.

    Args:
        receipt: The receipt dict produced by build_receipt().

    Returns:
        A UTF-8 JSON string matching brew's output format.
    """
    return dumps_bytes(receipt).decode("utf-8")


def write_receipt(keg_dir: Path, receipt: dict) -> Path:
    """Atomically write INSTALL_RECEIPT.json (mode 0644) into the keg root.

    Args:
        keg_dir: The keg directory to write the receipt into.
        receipt: The receipt dict produced by :func:`build_receipt`.

    Returns:
        The path to the written receipt file.
    """
    data = dumps_bytes(receipt)
    dest = keg_dir / RECEIPT_NAME
    fd, tmp = tempfile.mkstemp(dir=keg_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)

    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    return dest


def read_receipt(keg_dir: Path) -> dict | None:
    """Read INSTALL_RECEIPT.json from a keg, tolerating absence or corruption.

    The inverse of write_receipt, used to inherit fields from a superseded keg during an upgrade.

    Args:
        keg_dir: The keg directory to read the receipt from.

    Returns:
        The parsed receipt dict, or None if missing or unreadable.
    """
    try:
        data = orjson.loads((keg_dir / RECEIPT_NAME).read_bytes())

    except (OSError, orjson.JSONDecodeError):
        return None

    return data if isinstance(data, dict) else None


def current_arch() -> str:
    """brew's arch token: 'arm64' or 'x86_64'. Fallback for all-bottle receipts.

    Returns:
        `'arm64'` on Apple Silicon, otherwise the raw `platform.machine()` string.
    """
    machine = platform.machine()

    return "arm64" if machine == "arm64" else machine
