"""Shared logic for building Package objects from brew JSON data."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Coroutine, Optional

from brewery.analysis.status import derive_status
from brewery.core.models import Dependency, Package, PackageKind, PackageStatus
from brewery.core.shell import run_capture


async def _get_package_size(path: str | None) -> int | None:
    """Get the disk usage of an installed package in kilobytes.

    Args:
        path: The installation path of the package.

    Returns:
        Size in kilobytes, or None if the path doesn't exist or size can't be determined.
    """
    if not path:
        return None

    try:
        stdout, _, returncode = await run_capture("du", "-sk", path)
        if returncode == 0:
            size_kb = int(stdout.split()[0])
            return size_kb

    except (ValueError, IndexError, Exception):
        return None


async def _build_formula_package(formula_data: dict[str, Any]) -> Package:
    """Build a Package from formula JSON data.

    Args:
        formula_data: Single formula entry from `brew info --json=v2`

    Returns:
        Package instance with all fields populated.
    """
    f: dict[str, Any] = formula_data

    versions: list[str] = []
    installed: Any = f.get("installed", [])

    for v in installed:
        if ver := v.get("version"):
            versions.append(ver)

    latest: Any = f.get("versions", {}).get("stable") or f.get("versions", {}).get(
        "head"
    )
    if latest and (not versions or versions[-1] != latest):
        versions.append(latest)

    status: PackageStatus = derive_status(
        info={
            "outdated": f.get("outdated"),
            "pinned": f.get("pinned"),
            "keg_only": f.get("keg_only"),
            "linked_keg": f.get("linked_keg"),
            "installed": installed,
        }
    )

    deps: list[Dependency] = [Dependency(name=d) for d in f.get("dependencies", [])]

    installed_on = None
    if installed:
        t: Any = installed[-1].get("installed_time")
        if t:
            installed_on: datetime = datetime.fromtimestamp(t)

    path: Any = f.get("installed_path")
    if not path and installed:
        version: Any | None = installed[-1].get("version") if installed else None
        if version:
            path = f"/usr/local/Cellar/{f['name']}/{version}"

    size_kb: int | None = await _get_package_size(path) if installed else None

    return Package(
        name=f["name"],
        kind=PackageKind.FORMULA,
        versions=versions,
        desc=f.get("desc"),
        status=status,
        installed_on=installed_on,
        size_kb=size_kb,
        deps=deps,
        tap=f.get("tap"),
        path=path,
        metadata={"latest_version": latest},
    )


async def _build_cask_package(
    cask_data: dict[str, Any],
    caskroom_path: str,
) -> Package:
    """Build a Package from cask JSON data.

    Args:
        cask_data: Single cask entry from `brew info --json=v2 --cask`
        caskroom_path: Path to caskroom directory

    Returns:
        Package instance with all fields populated.
    """
    c: dict[str, Any] = cask_data

    version_value: Any = c.get("version")
    versions: list[str] = [str(object=version_value)] if version_value else []

    status: PackageStatus = derive_status(
        info={
            "outdated": c.get("outdated"),
            "pinned": c.get("pinned"),
            "keg_only": c.get("keg_only"),
            "linked_keg": c.get("linked_keg"),
            "installed": c.get("installed"),
        }
    )

    token: Any = c.get("token") or c.get("name", [None])[0]
    cask_path: str | None = f"{caskroom_path}/{token}" if token else None

    size_kb: int | None = await _get_package_size(path=cask_path)

    return Package(
        name=token,
        kind=PackageKind.CASK,
        versions=versions,
        desc=c.get("desc") or "",
        status=status,
        size_kb=size_kb,
        path=cask_path,
        metadata={"latest_version": c.get("version"), "tap": c.get("tap")},
    )


async def build_packages_batch(
    items: list[dict[str, Any]],
    kind: PackageKind,
    caskroom_path: Optional[str] = None,
) -> list[Package]:
    """Build multiple packages in parallel.

    Args:
        items: List of formula or cask JSON entries
        kind: PackageKind.FORMULA or PackageKind.CASK
        caskroom_path: Required for CASK, optional for FORMULA

    Returns:
        List of Package instances.
    """
    if kind == PackageKind.FORMULA:
        tasks: list[Coroutine] = [
            _build_formula_package(formula_data=item) for item in items
        ]

    else:
        if not caskroom_path:
            raise ValueError("caskroom_path required for cask packages")
        tasks: list[Coroutine] = [
            _build_cask_package(cask_data=item, caskroom_path=caskroom_path)
            for item in items
        ]

    return await asyncio.gather(*tasks)
