"""Shared logic for building Package objects from brew JSON data."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Coroutine, Optional

from brewery.analysis.status import StatusInfo, derive_status
from brewery.core.config import get_brewery_env
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import Dependency, Package, PackageKind, PackageStatus
from brewery.core.shell import run_capture, run_json

log: BreweryLogger = get_logger()

_BATCH_SIZE = 30
_SEMAPHORE_SIZE = asyncio.Semaphore(5)


async def _get_package_size(path: str | None) -> int | None:
    """Get the disk usage of an installed package in kilobytes.

    Args:
        path: The installation path of the package.

    Returns:
        Size in kilobytes, or None if the path doesn't exist or size can't be determined.
    """
    if not path:
        log.warning(event="package_size_skipped", reason="no_path")
        return None

    async with _SEMAPHORE_SIZE:
        try:
            stdout, stderr, returncode = await run_capture("du", "-sk", path)
            if returncode == 0:
                return int(stdout.split()[0])

            log.warning(
                event="package_size_failed",
                path=path,
                returncode=returncode,
                stderr=stderr,
            )
            return None

        except ValueError as e:
            log.warning(
                event="package_size_parse_error",
                path=path,
                error=str(e),
            )
            return None

        except IndexError as e:
            log.warning(
                event="package_size_index_error",
                path=path,
                error=str(e),
            )
            return None

        except Exception as e:
            log.warning(
                event="package_size_unexpected_error",
                path=path,
                error=str(e),
                exc_info=True,
            )
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
    installed: list[dict[str, Any]] = f.get("installed", [])

    for v in installed:
        if ver := v.get("version"):
            versions.append(ver)

    latest: Any = f.get("versions", {}).get("stable") or f.get("versions", {}).get(
        "head"
    )
    if latest and installed and (not versions or versions[-1] != latest):
        versions.append(latest)

    status: PackageStatus = derive_status(
        info=StatusInfo(
            outdated=f.get("outdated"),
            pinned=f.get("pinned"),
            keg_only=f.get("keg_only"),
            linked_keg=f.get("linked_keg"),
            installed=installed,
        )
    )

    deps: list[Dependency] = [Dependency(name=d) for d in f.get("dependencies", [])]

    installed_on: datetime | None = None
    if installed:
        if ts := installed[-1].get("time"):
            installed_on = datetime.fromtimestamp(int(ts))

    path: Any = f.get("installed_path")
    if not path and installed:
        version: str | None = installed[-1].get("version")
        if version:
            path = str(get_brewery_env().cellar / f["name"] / version)

    if installed and not path:
        log.warning(event="formula_path_unresolved", name=f["name"])

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

    installed: str | None = c.get("installed")

    version_value: str | None = c.get("version")
    versions: list[str] = [installed] if installed else []
    if version_value and (not versions or versions[-1] != version_value):
        versions.append(version_value)

    installed_on: datetime | None = None
    if raw_ts := c.get("installed_time"):
        try:
            installed_on = datetime.fromtimestamp(float(raw_ts))
        except (ValueError, OSError):
            pass

    status: PackageStatus = derive_status(
        info=StatusInfo(
            outdated=c.get("outdated"),
            pinned=c.get("pinned"),
            keg_only=c.get("keg_only"),
            linked_keg=c.get("linked_keg"),
            installed=installed,
        )
    )

    token: str | None = c.get("token") or c.get("name", [None])[0]
    if not token:
        raise ValueError(f"Could not determine token for cask: {c}")

    cask_path: str = f"{caskroom_path}/{token}"
    size_kb: int | None = await _get_package_size(path=cask_path)

    return Package(
        name=token,
        kind=PackageKind.CASK,
        versions=versions,
        desc=c.get("desc"),
        status=status,
        installed_on=installed_on,
        size_kb=size_kb,
        path=cask_path,
        metadata={"latest_version": version_value, "tap": c.get("tap")},
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


async def batch_info(
    names: list[str],
    flags: list[str],
    json_key: str,
    kind: PackageKind,
    caskroom_path: Optional[str] = None,
) -> list[Package]:
    """Fetch and build Package objects for named packages in batches.

    Args:
        names: Package names to look up.
        flags: Extra flags for `brew info` (e.g. ["--cask"]).
        json_key: Key in the JSON response containing results ("formulae" or "casks").
        kind: Package kind, forwarded to build_packages_batch.
        caskroom_path: Required when kind is CASK.

    Returns:
        List of Package instances for all named packages found.
    """
    pkgs: list[Package] = []
    for i in range(0, len(names), _BATCH_SIZE):
        batch: list[str] = names[i : i + _BATCH_SIZE]
        data: dict[str, Any] = await run_json(
            "brew", "info", "--json=v2", *flags, *batch
        )
        items: Any = data.get(json_key, [])
        batch_pkgs: list[Package] = await build_packages_batch(
            items=items, kind=kind, caskroom_path=caskroom_path
        )
        pkgs.extend(batch_pkgs)

    return pkgs
