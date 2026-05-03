"""Homebrew Cask provider."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, List

from structlog.typing import FilteringBoundLogger

from brewery.analysis.status import derive_status

if TYPE_CHECKING:
    from ty_extensions import Unknown
from brewery.core.errors import PackageNotFoundError
from brewery.core.logging import get_logger
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.core.shell import run_brew_command, run_capture, run_json

log: FilteringBoundLogger = get_logger(name=__name__)

BATCH_SIZE = 30


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
    """List installed Homebrew casks.

    Returns:
        A list of installed Package instances.
    """
    start: int | float = time.perf_counter()
    log.debug(event="cask_list_start")

    out, _, _ = await run_capture("brew", "list", "--cask")
    names: list[str] = [name.strip() for name in out.split(sep="\n") if name.strip()]
    pkgs: List[Package] = []
    log.debug(event="cask_list_names", count=len(names))

    caskroom_out, _, caskroom_code = await run_capture("brew", "--caskroom")
    caskroom_path = (
        caskroom_out.strip() if caskroom_code == 0 else "/usr/local/Caskroom"
    )

    for i in range(0, len(names), BATCH_SIZE):
        batch: list[str] = names[i : i + BATCH_SIZE]
        data: Any = await run_json("brew", "info", "--json=v2", "--cask", *batch)
        items: Any = data.get("casks", [])

        for c in items:
            version_value: Any = c.get("version")
            versions: list[str] | list[Unknown] = (
                [str(object=version_value)] if version_value else []
            )

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

            size_kb: int | None = await get_package_size(path=cask_path)

            pkg = Package(
                name=token,
                kind=PackageKind.CASK,
                versions=versions,
                desc=(c.get("desc") or ""),
                status=status,
                size_kb=size_kb,
                path=cask_path,
                metadata={"latest_version": c.get("version"), "tap": c.get("tap")},
            )

            pkgs.append(pkg)

    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(event="cask_list_complete", count=len(pkgs), duration_ms=duration_ms)

    return pkgs


async def info(name: str) -> Package:
    """Get cask info by name.

    Args:
        name: Name of the cask.

    Returns:
        A Package instance with detailed information.
    """
    start: int | float = time.perf_counter()
    log.debug(event="cask_info_start", package=name)

    data: Any = await run_json("brew", "info", "--json=v2", "--cask", name)
    c: Any | dict[Unknown, Unknown] = (data.get("casks", []) or [{}])[0]
    if not c:
        log.error(event="cask_not_found", package=name)
        raise PackageNotFoundError(package=name, kind="cask")

    version_value: Any | None = c.get("version")
    versions: list[str] | list[Unknown] = (
        [str(object=version_value)] if version_value else []
    )

    status: PackageStatus = derive_status(
        info={
            "outdated": c.get("outdated"),
            "pinned": c.get("pinned"),
            "keg_only": c.get("keg_only"),
            "linked_keg": c.get("linked_keg"),
            "installed": c.get("installed"),
        }
    )

    caskroom_out, _, caskroom_code = await run_capture("brew", "--caskroom")
    caskroom_path: str = (
        caskroom_out.strip() if caskroom_code == 0 else "/usr/local/Caskroom"
    )

    token: Any = c.get("token") or c.get("name", [None])[0]
    cask_path: str | None = f"{caskroom_path}/{token}" if token else None

    size_kb: int | None = (
        await get_package_size(path=cask_path) if c.get("installed") else None
    )

    pkg = Package(
        name=token,
        kind=PackageKind.CASK,
        versions=versions,
        desc=(c.get("desc") or ""),
        status=status,
        size_kb=size_kb,
        path=cask_path,
        metadata={"latest_version": c.get("version"), "tap": c.get("tap")},
    )

    duration_ms = int((time.perf_counter() - start) * 1000)
    log.info(event="cask_info_complete", package=name, duration_ms=duration_ms)

    return pkg


async def install(name: str) -> str:
    """Install a Homebrew cask by name.

    Args:
        name: Name of the cask to install.

    Returns:
        The cask name on success.

    Raises:
        BrewCommandError: If the installation fails.
    """
    await run_brew_command(subcommand="install", name=name, flags=["--cask"])

    return name


async def uninstall(name: str) -> str:
    """Uninstall a Homebrew cask by name.

    Args:
        name: Name of the cask to uninstall.

    Returns:
        The cask name on success.

    Raises:
        BrewCommandError: If the uninstallation fails.
    """
    await run_brew_command(subcommand="uninstall", name=name, flags=["--cask"])

    return name
