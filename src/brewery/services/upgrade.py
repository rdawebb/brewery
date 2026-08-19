"""Upgrade formulae and casks, reporting what moved and what was already current."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from brewery.core.config import BreweryENV
from brewery.core.decorators import log_operation
from brewery.core.models import Notes, Package, PackageKind, PackageStatus
from brewery.core.repo import Repository
from brewery.core.shell import run_brew
from brewery.providers import brew
from brewery.providers.base import PackageBackend
from brewery.providers.pipeline import run_upgrade
from brewery.services.cask import upgrade_casks

if TYPE_CHECKING:
    from brewery.providers.orchestrator import ProgressPort

UpgradeOutcome = tuple[list[Package], list[Package], Notes, Notes]


def _select_targets(
    repo: Repository, installed: list[Package], names: list[str] | None
) -> tuple[list[Package], list[Package], Notes, Notes]:
    """Resolve the packages to upgrade, and what to say about the ones skipped.

    Args:
        repo: The data facade, for alias resolution.
        installed: Every installed package, already merged.
        names: Explicit targets, or None for "everything outdated".

    Returns:
        Tuple of (targets, already-current packages, advisories, failures).
    """
    advisories: Notes = []

    # Bulk upgrade: everything outdated, skipping pins without failing
    if names is None:
        targets = [p for p in installed if PackageStatus.OUTDATED in p.status]
        advisories += [
            (p.name, "pinned - not upgraded")
            for p in targets
            if PackageStatus.PINNED in p.status
        ]

        return (
            [p for p in targets if PackageStatus.PINNED not in p.status],
            [],
            advisories,
            [],
        )

    # Named upgrade
    by_name: dict[str, Package] = {p.name: p for p in installed}
    resolved: dict[str, str] = {n: repo.catalog.resolve_alias(n) for n in names}
    targets = [by_name[resolved[n]] for n in names if resolved[n] in by_name]
    failures: Notes = [(n, "not found") for n in names if resolved[n] not in by_name]

    # Naming a pinned package explicitly is a failure
    failures += [
        (p.name, "pinned - skipped")
        for p in targets
        if PackageStatus.PINNED in p.status
    ]
    targets = [p for p in targets if PackageStatus.PINNED not in p.status]

    # The orchestrator forces requested targets past `is_satisfied`, so a current
    # formula would otherwise be re-poured in full; casks are exempt because
    # nothing derives OUTDATED for them yet (see dev/open.md #3 -- revisit this
    # line when cask outdated lands)
    satisfied = [
        p
        for p in targets
        if p.kind == PackageKind.FORMULA and PackageStatus.OUTDATED not in p.status
    ]
    skip = {p.name for p in satisfied}

    return [p for p in targets if p.name not in skip], satisfied, advisories, failures


@log_operation(event_prefix="upgrade_packages", log_args=["names", "kind"])
async def upgrade_packages(
    repo: Repository,
    names: list[str] | None = None,
    kind: PackageKind | None = None,
    *,
    env: BreweryENV | None = None,
    formula: PackageBackend = brew.formula_backend,
    cask: PackageBackend = brew.cask_backend,
    progress: ProgressPort | None = None,
) -> UpgradeOutcome:
    """Upgrade packages and report upgraded, up-to-date, advisories, and failures.

    Naming a formula that is already current reports it as up-to-date rather
    than reinstalling it.

    Args:
        repo: The data facade to read installed state and aliases through.
        names: Name(s) of the package(s) to upgrade.
        kind: Kind of the package(s) (formula, cask, auto (default))
        env: Brewery environment (paths), resolved by the pipeline if omitted.
        formula: Formula backend for the per-formula brew fallback.
        cask: Cask backend, which handles casks wholesale.
        progress: Optional progress sink for the native pipeline.

    Returns:
        Tuple of (upgraded packages, already up-to-date packages, (name, reason)
        advisories, (name, reason) failures).

    Raises:
        BrewCommandError: Propagated from provider.
    """
    installed: list[Package] = repo.cache_mgr.installed_packages()
    targets, satisfied, advisories, failures = _select_targets(repo, installed, names)

    if kind is not None:
        targets = [p for p in targets if p.kind == kind]
        satisfied = [p for p in satisfied if p.kind == kind]

    formula_names = [p.name for p in targets if p.kind == PackageKind.FORMULA]
    cask_names = [p.name for p in targets if p.kind == PackageKind.CASK]
    pre_versions: dict[str, str | None] = {
        p.name: (p.versions[0] if p.versions else None) for p in (*targets, *satisfied)
    }

    if formula_names:
        old_kegs = {
            p.name: Path(p.path)
            for p in targets
            if p.kind == PackageKind.FORMULA and p.path
        }
        await run_upgrade(
            formula_names,
            old_kegs,
            catalog=repo.catalog,
            cache_mgr=repo.cache_mgr,
            formula=formula,
            run_brew=run_brew,
            env=env,
            progress=progress,
        )

    if cask_names:
        await upgrade_casks(cask_names, backend=cask)

    # Only invalidate the cache if something actually changed
    if formula_names or cask_names:
        repo.cache_mgr.invalidate()

    post: dict[str, Package] = {p.name: p for p in repo.cache_mgr.installed_packages()}

    upgraded: list[Package] = []
    current: list[Package] = []
    for name in formula_names + cask_names + [p.name for p in satisfied]:
        pkg = post.get(name)
        if pkg is None:
            continue

        new_version = pkg.versions[0] if pkg.versions else None
        if new_version != pre_versions.get(name):
            upgraded.append(pkg)
        else:
            current.append(pkg)

    return upgraded, current, advisories, failures
