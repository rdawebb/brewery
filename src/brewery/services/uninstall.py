"""Uninstall formulae and casks, verifying removal against the filesystem."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.decorators import log_operation
from brewery.core.deps import blocking_dependents
from brewery.core.errors import BrewCommandError, OperationInProgressError
from brewery.core.fs_state import child_dirs
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import Notes, Package, PackageKind
from brewery.core.repo import Repository
from brewery.providers import brew
from brewery.providers.base import PackageBackend, UninstallBackend
from brewery.providers.cellar import remove_rack
from brewery.services.cask import uninstall_casks

log: BreweryLogger = get_logger(name=__name__)


@log_operation(event_prefix="uninstall_package", log_args=["names", "kind"])
async def uninstall_packages(
    repo: Repository,
    names: list[str],
    kind: PackageKind | None = None,
    *,
    env: BreweryENV | None = None,
    formula: UninstallBackend = brew.formula_backend,
    cask: PackageBackend = brew.cask_backend,
) -> tuple[list[str], Notes]:
    """Uninstall packages and refresh cache on success.

    Args:
        repo: The data facade to read installed state and aliases through.
        names: Name(s) of the package(s) to uninstall.
        kind: Kind of the package(s) (formula or cask).
        env: Brewery environment (paths), resolved if omitted.
        formula: Formula backend for the per-formula brew fallback.
        cask: Cask backend, which handles casks wholesale.

    Returns:
        List of successfully removed package names, and list of (name, reason) failures

    Raises:
        BrewCommandError: Propagated from provider.
    """
    env = env or repo.cache_mgr.env or get_brewery_env()
    resolved: dict[str, str] = {n: repo.catalog.resolve_alias(n) for n in names}

    all_pkgs: list[Package] | None = None

    if kind is None:
        # Resolve kinds and split into two lists
        all_pkgs = repo.get_all_installed()
        kind_map: dict[str, PackageKind] = {p.name: p.kind for p in all_pkgs}
        formula_names: list[str] = [
            resolved[n]
            for n in names
            if kind_map.get(resolved[n]) == PackageKind.FORMULA
        ]

        cask_names: list[str] = [
            resolved[n] for n in names if kind_map.get(resolved[n]) == PackageKind.CASK
        ]

        failures: Notes = [
            (n, "not found") for n in names if resolved[n] not in kind_map
        ]

    else:
        formula_names: list[str] = (
            [resolved[n] for n in names] if kind == PackageKind.FORMULA else []
        )
        cask_names: list[str] = (
            [resolved[n] for n in names] if kind == PackageKind.CASK else []
        )
        failures: Notes = []

    blocked: dict[str, list[str]] = {}
    if formula_names:
        # Reuse the scan above when there was one; a cask-only batch never scans
        source = (
            all_pkgs
            if all_pkgs is not None
            else repo.cache_mgr.installed_packages(kind=PackageKind.FORMULA)
        )
        blocked = blocking_dependents(source, set(formula_names))

    if blocked:
        failures.extend(
            (name, f"required by {', '.join(deps)}") for name, deps in blocked.items()
        )
        formula_names = [n for n in formula_names if n not in blocked]

    if formula_names:
        await run_uninstall(formula_names, formula=formula, env=env)

    if cask_names:
        await uninstall_casks(cask_names, backend=cask)

    repo.cache_mgr.invalidate()

    removed: list[str] = []
    failed: list[str] = []

    for pkg_names, k in [
        (formula_names, PackageKind.FORMULA),
        (cask_names, PackageKind.CASK),
    ]:
        if not pkg_names:
            continue

        r, f = _verify_removed(pkg_names, k, env=env)
        removed += r
        failed += f

    failures.extend((n, "uninstall failed") for n in failed)

    return removed, failures


async def run_uninstall(
    names: list[str],
    *,
    formula: UninstallBackend,
    env: BreweryENV | None = None,
) -> None:
    """Unlink + remove each formula's kegs, brew-falling-back per formula.

    Args:
        names: Canonical formula names to uninstall.
        formula: Formula backend for the per-formula brew fallback.
        env: Brewery environment (paths), resolved if omitted.
    """
    env = env or get_brewery_env()
    for name in names:
        try:
            await asyncio.to_thread(remove_rack, env.cellar / name, env.prefix, name)

        except OperationInProgressError as exc:
            # brew locks the same rack, so falling back to it would fail too
            log.warning(event="uninstall_rack_locked", formula=name, error=str(exc))

        except OSError:
            try:
                await formula.uninstall(names=[name])

            except BrewCommandError:
                pass  # Verification reports the survivor as a failure


def _verify_removed(
    names: list[str], kind: PackageKind, *, env: BreweryENV
) -> tuple[list[str], list[str]]:
    """Return (removed, failed) based on filesystem presence.

    A package counts as installed only while its directory still holds a version.
    The lookup is case-insensitive so a mixed-case cask token still finds its
    directory on a case-sensitive volume.

    Args:
        names: Package names to verify.
        kind: Package kind (formula or cask), selecting cellar or caskroom.
        env: Brewery environment (paths).

    Returns:
        Tuple of (removed, failed) package names.
    """
    base_dir = env.cellar if kind == PackageKind.FORMULA else env.caskroom
    index: dict[str, Path] = {d.name.casefold(): d for d in child_dirs(base_dir)}

    removed, failed = [], []
    for name in names:
        survivor: Path | None = index.get(name.casefold())

        if survivor is not None and child_dirs(survivor):
            failed.append(name)
            continue

        if survivor is not None:
            # Nothing installed under it; drop the shell so the tree matches
            with contextlib.suppress(OSError):
                survivor.rmdir()

        removed.append(name)

    return removed, failed
