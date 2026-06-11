"""Write a brew-compatible `INSTALL_RECEIPT.json` (the keg "tab")."""

from __future__ import annotations

import json
import os
import platform
import tempfile
from dataclasses import dataclass
from pathlib import Path

RECEIPT_NAME = "INSTALL_RECEIPT.json"


@dataclass(frozen=True)
class RuntimeDependency:
    full_name: str
    version: str
    revision: int = 0
    bottle_rebuild: int = 0
    pkg_version: str | None = None  # Defaults to `version`
    declared_directly: bool = False

    @classmethod
    def from_tab(cls, dep: dict) -> "RuntimeDependency":
        """Build from a tab runtime_dependency, ignoring compatibility_version."""
        return cls(
            full_name=dep["full_name"],
            version=dep["version"],
            revision=dep.get("revision", 0),
            bottle_rebuild=dep.get("bottle_rebuild", 0),
            pkg_version=dep.get("pkg_version"),
            declared_directly=dep.get("declared_directly", False),
        )

    def to_dict(self) -> dict:
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
    # Manifest
    homebrew_version: str,
    changed_files: list[str],
    source_modified_time: int,
    compiler: str,
    runtime_dependencies: list[RuntimeDependency],
    built_on: dict | None,
    arch: str | None,
    # Host/install
    installed_on_request: bool,
    time: int,
    # Catalog
    source: Source,
    aliases: list[str],
    # Constants (overridable)
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
        "arch": arch if arch is not None else current_arch(),
        "built_on": built_on,
    }


def dumps(receipt: dict) -> str:
    """Serialise exactly as brew does: 2-space indent, key order preserved,
    no trailing newline (Ruby JSON.pretty_generate)."""
    return json.dumps(receipt, indent=2, ensure_ascii=False)


def write_receipt(keg_dir: Path, receipt: dict) -> Path:
    """Atomically write INSTALL_RECEIPT.json (mode 0644) into the keg root."""
    text = dumps(receipt)
    dest = keg_dir / RECEIPT_NAME
    fd, tmp = tempfile.mkstemp(dir=keg_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest


def current_arch() -> str:
    """brew's arch token: 'arm64' or 'x86_64'. Fallback for all-bottle receipts."""
    machine = platform.machine()
    return "arm64" if machine == "arm64" else machine
