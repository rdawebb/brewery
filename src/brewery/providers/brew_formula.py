"""Homebrew formula provider."""

from __future__ import annotations

import time
from datetime import datetime
from typing import List

from brewery.analysis.status import derive_status
from brewery.core.errors import PackageNotFoundError
from brewery.core.logging import get_logger
from brewery.core.models import Dependency, Package, PackageKind
from brewery.core.shell import run_json

log = get_logger(__name__)


async def list_installed() -> List[Package]:
    """List installed Homebrew formulae.
    
    Returns:
        A list of installed Package instances.
    """
    start = time.perf_counter()
    log.debug("formula_list_start")

    data = await run_json("brew", "info", "--json=v2", "--installed")
    items = data.get("formulae", [])
    pkgs: List[Package] = []

    for f in items:
        versions = []
        installed = f.get("installed", [])
        for v in installed:
            if ver := v.get("version"):
                versions.append(ver)
        
        latest = f.get("versions", {}).get("stable") or f.get("versions", {}).get("head")
        if latest and (not versions or versions[-1] != latest):
            versions.append(latest)

        status = derive_status({
            "outdated": f.get("outdated"),
            "pinned": f.get("pinned"),
            "keg_only": f.get("keg_only"),
            "linked_keg": f.get("linked_keg"),
            "installed": installed,
        })

        deps = [Dependency(name=d) for d in (f.get("dependencies", []))]
        installed_on = None
        if installed:
            t = installed[-1].get("installed_time")
            if t:
                installed_on = datetime.fromtimestamp(t)
        
        pkg = Package(
            name=f["name"],
            kind=PackageKind.FORMULA,
            versions=versions,
            desc=f.get("desc"),
            status=status,
            installed_on=installed_on,
            deps=deps,
            tap=f.get("tap"),
            path=f.get("installed_path"),
            metadata={"latest_version": latest}
        )

        pkgs.append(pkg)

    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(
        "formula_list_complete",
        count=len(pkgs),
        duration_ms=duration_ms
    )

    return pkgs

async def info(name: str) -> Package:
    """Get Homebrew formula info by name.
    
    Args:
        name: Name of the formula.
        
    Returns:
        A Package instance with detailed information.
    """
    start = time.perf_counter()
    log.debug("formula_info_start", package=name)

    data = await run_json("brew", "info", "--json=v2", name)
    f = (data.get("formulae") or [{}])[0]
    if not f:
        log.error("formula_not_found", package=name)
        raise PackageNotFoundError(
            package=name,
            kind="formula"
        )

    pkg = (await list_installed_from_items([f]))[0]
    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(
        "formula_info_complete",
        package=name,
        duration_ms=duration_ms
    )

    return pkg

async def list_installed_from_items(items) -> List[Package]:
    """Helper to list installed packages from given items.
    
    Args:
        items: List of formula data items.
        
    Returns:
        A list of installed Package instances.
    """
    pkgs: List[Package] = []

    for f in items:
        versions = []
        installed = f.get("installed", [])
        for v in installed:
            if ver := v.get("version"):
                versions.append(ver)
        
        latest = f.get("versions", {}).get("stable") or f.get("versions", {}).get("head")
        if latest and (not versions or versions[-1] != latest):
            versions.append(latest)

        status = derive_status({
            "outdated": f.get("outdated"),
            "pinned": f.get("pinned"),
            "keg_only": f.get("keg_only"),
            "linked_keg": f.get("linked_keg"),
            "installed": installed,
        })

        deps = [Dependency(name=d) for d in (f.get("dependencies", []))]
        installed_on = None
        if installed:
            t = installed[-1].get("installed_time")
            if t:
                installed_on = datetime.fromtimestamp(t)
        
        pkg = Package(
            name=f["name"],
            kind=PackageKind.FORMULA,
            versions=versions,
            desc=f.get("desc"),
            status=status,
            installed_on=installed_on,
            deps=deps,
            tap=f.get("tap"),
            path=f.get("installed_path"),
            metadata={"latest_version": latest}
        )

        pkgs.append(pkg)

    return pkgs