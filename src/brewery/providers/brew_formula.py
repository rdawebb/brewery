"""Homebrew formula provider."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, List

from structlog.typing import FilteringBoundLogger

if TYPE_CHECKING:
    from ty_extensions import Unknown

from brewery.analysis.status import derive_status
from brewery.core.errors import PackageNotFoundError
from brewery.core.logging import get_logger
from brewery.core.models import Dependency, Package, PackageKind, PackageStatus
from brewery.core.shell import run_brew_command, run_capture, run_json

log: FilteringBoundLogger = get_logger(name=__name__)


async def get_package_size(path: str | None) -> int | None:
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
    except (ValueError, IndexError, Exception) as e:
        log.debug(event="get_size_error", path=path, error=str(object=e))

    return None


async def list_installed() -> List[Package]:
    """List installed Homebrew formulae.

    Returns:
        A list of installed Package instances.
    """
    start: int | float = time.perf_counter()
    log.debug(event="formula_list_start")

    data: Any = await run_json("brew", "info", "--json=v2", "--installed")
    items: Any = data.get("formulae", [])
    pkgs: List[Package] = []

    for f in items:
        versions: list[Unknown] = []
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

        deps: list[Dependency] = [
            Dependency(name=d) for d in (f.get("dependencies", []))
        ]
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

        size_kb: int | None = await get_package_size(path) if installed else None

        pkg = Package(
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

        pkgs.append(pkg)

    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(event="formula_list_complete", count=len(pkgs), duration_ms=duration_ms)

    return pkgs


async def info(name: str) -> Package:
    """Get Homebrew formula info by name.

    Args:
        name: Name of the formula.

    Returns:
        A Package instance with detailed information.
    """
    start: int | float = time.perf_counter()
    log.debug(event="formula_info_start", package=name)

    data: Any = await run_json("brew", "info", "--json=v2", name)
    f: Any | dict[Unknown, Unknown] = (data.get("formulae") or [{}])[0]
    if not f:
        log.error(event="formula_not_found", package=name)
        raise PackageNotFoundError(package=name, kind="formula")

    pkg: Package = (await list_installed_from_items(items=[f]))[0]
    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(event="formula_info_complete", package=name, duration_ms=duration_ms)

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
        versions: list[Unknown] = []
        installed: Unknown = f.get("installed", [])
        for v in installed:
            if ver := v.get("version"):
                versions.append(ver)

        latest: Unknown = f.get("versions", {}).get("stable") or f.get(
            "versions", {}
        ).get("head")
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

        deps: list[Dependency] = [
            Dependency(name=d) for d in (f.get("dependencies", []))
        ]
        installed_on = None
        if installed:
            t: Unknown = installed[-1].get("installed_time")
            if t:
                installed_on: datetime = datetime.fromtimestamp(t)

        path: Unknown = f.get("installed_path")
        if not path and installed:
            version: Unknown | None = (
                installed[-1].get("version") if installed else None
            )
            if version:
                path = f"/usr/local/Cellar/{f['name']}/{version}"

        size_kb: int | None = await get_package_size(path) if installed else None

        pkg = Package(
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

        pkgs.append(pkg)

    return pkgs


async def install(name: str) -> str:
    """Install a Homebrew formula by name.

    Args:
        name: Name of the formula to install.

    Returns:
        The package name on success.

    Raises:
        BrewCommandError: If the installation fails.
    """
    await run_brew_command("install", name, flags=["--formula"])

    return name


async def uninstall(name: str) -> str:
    """Uninstall a Homebrew formula by name.

    Args:
        name: Name of the formula to uninstall.

    Returns:
        The package name on success.

    Raises:
        BrewCommandError: If the uninstallation fails.
    """
    await run_brew_command("uninstall", name, flags=["--formula"])

    return name
