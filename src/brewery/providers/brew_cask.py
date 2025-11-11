"""Homebrew Cask provider."""

from __future__ import annotations

import time
from typing import List

from brewery.core.errors import PackageNotFoundError
from brewery.core.logging import get_logger
from brewery.core.models import Package, PackageKind
from brewery.core.shell import run_capture, run_json

log = get_logger(__name__)

BATCH_SIZE = 30


async def list_installed() -> List[Package]:
    """List installed Homebrew casks.

    Returns:
        A list of installed Package instances.
    """
    start = time.perf_counter()
    log.debug("cask_list_start")

    out, _, _ = await run_capture("brew", "list", "--cask")
    names = [name.strip() for name in out.split("\n") if name.strip()]
    pkgs: List[Package] = []
    log.debug("cask_list_names", count=len(names))

    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i : i + BATCH_SIZE]
        data = await run_json("brew", "info", "--json=v2", "--cask", *batch)
        items = data.get("casks", [])

        for c in items:
            versions = [c.get("version")] if c.get("version") else []

            pkg = Package(
                name=c.get("token") or c.get("name", [None])[0],
                kind=PackageKind.CASK,
                versions=versions,
                desc=(c.get("desc") or ""),
                metadata={"latest_version": c.get("version"), "tap": c.get("tap")},
            )

            pkgs.append(pkg)
    
    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(
        "cask_list_complete",
        count=len(pkgs),
        duration_ms=duration_ms
    )

    return pkgs

async def info(name: str) -> Package:
    """Get cask info by name.
    
    Args:
        name: Name of the cask.
        
    Returns:
        A Package instance with detailed information.
    """
    start = time.perf_counter()
    log.debug("cask_info_start", package=name)

    data = await run_json("brew", "info", "--json=v2", "--cask", name)
    c =(data.get("casks", []) or [{}])[0]
    if not c:
        log.error("cask_not_found", package=name)
        raise PackageNotFoundError(
            package=name,
            kind="cask"
        )

    versions = [c.get("version")] if c.get("version") else []

    pkg = Package(
        name=c.get("token") or c.get("name", [None])[0],
        kind=PackageKind.CASK,
        versions=versions,
        desc=(c.get("desc") or ""),
        metadata={"latest_version": c.get("version"), "tap": c.get("tap")},
    )

    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(
        "cask_info_complete",
        package=name,
        duration_ms=duration_ms
    )

    return pkg