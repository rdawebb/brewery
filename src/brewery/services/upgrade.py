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


def _select_outdated(installed: list[Package]) -> tuple[list[Package], Notes]:
    """Everything outdated, skipping pins without failing.

    Args:
        installed: Every installed package, already merged.

    Returns:
        Tuple of (targets, advisories) - one advisory per pin left behind.
    """
    outdated = [p for p in installed if PackageStatus.OUTDATED in p.status]
    advisories: Notes = [
        (p.name, "pinned - not upgraded")
        for p in outdated
        if PackageStatus.PINNED in p.status
    ]

    return [p for p in outdated if PackageStatus.PINNED not in p.status], advisories


def _select_named(
    repo: Repository, installed: list[Package], names: list[str]
) -> tuple[list[Package], list[Package], Notes]:
    """Resolve explicitly named targets, holding back pins and current formulae.

    Args:
        repo: The data facade, for alias resolution.
        installed: Every installed package, already merged.
        names: The names the caller asked for.

    Returns:
        Tuple of (targets, already-current packages, failures).
    """
    by_name: dict[str, Package] = {p.name: p for p in installed}
    resolved: dict[str, str] = {n: repo.catalog.resolve_alias(n) for n in names}
    targets = [by_name[resolved[n]] for n in names if resolved[n] in by_name]
    failures: Notes = [(n, "not found") for n in names if resolved[n] not in by_name]

    targets, pinned = _drop_pinned(targets)
    targets, satisfied = _drop_satisfied(targets)

    return targets, satisfied, failures + pinned


def _drop_pinned(targets: list[Package]) -> tuple[list[Package], Notes]:
    """Hold back pinned targets. Naming one explicitly is a failure, not a skip.

    Args:
        targets: The named packages resolved against installed state.

    Returns:
        The unpinned targets, and a (name, reason) failure for each pin.
    """
    failures: Notes = [
        (p.name, "pinned - skipped")
        for p in targets
        if PackageStatus.PINNED in p.status
    ]

    return [p for p in targets if PackageStatus.PINNED not in p.status], failures


def _drop_satisfied(targets: list[Package]) -> tuple[list[Package], list[Package]]:
    """Hold back formulae that are already current, to report rather than re-pour.

    The orchestrator forces requested targets past `is_satisfied`, so a current
    formula would otherwise be re-poured in full.

    Args:
        targets: The named packages still bound for upgrade.

    Returns:
        Tuple of (targets still worth upgrading, already-current packages).
    """
    satisfied = [
        p
        for p in targets
        if p.kind == PackageKind.FORMULA and PackageStatus.OUTDATED not in p.status
    ]
    skip = {p.name for p in satisfied}

    return [p for p in targets if p.name not in skip], satisfied


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
    if names is None:
        targets, advisories = _select_outdated(installed)

        return targets, [], advisories, []

    targets, satisfied, failures = _select_named(repo, installed, names)

    return targets, satisfied, [], failures


def _filter_kind(
    targets: list[Package], satisfied: list[Package], kind: PackageKind | None
) -> tuple[list[Package], list[Package]]:
    """Restrict the batch to one kind, or pass it through when unfiltered.

    Args:
        targets: The packages selected for upgrade.
        satisfied: The packages already found current.
        kind: The kind to keep, or None for both.

    Returns:
        The two lists, filtered.
    """
    if kind is None:
        return targets, satisfied

    return (
        [p for p in targets if p.kind == kind],
        [p for p in satisfied if p.kind == kind],
    )


def _active_version(pkg: Package) -> str | None:
    """The package's active version, or None when it records none.

    Args:
        pkg: The package to read.

    Returns:
        The first recorded version, or None.
    """
    return pkg.versions[0] if pkg.versions else None


def _old_kegs(targets: list[Package]) -> dict[str, Path]:
    """The keg path each formula target is being upgraded away from.

    Args:
        targets: The packages selected for upgrade.

    Returns:
        Keg path by formula name, skipping targets with no recorded path.
    """
    return {
        p.name: Path(p.path)
        for p in targets
        if p.kind == PackageKind.FORMULA and p.path
    }


def _classify_outcome(
    touched: list[str], pre_versions: dict[str, str | None], post: dict[str, Package]
) -> tuple[list[Package], list[Package]]:
    """Split the packages the run touched by whether their version actually moved.

    Args:
        touched: Every name the run handled, including ones found already current.
        pre_versions: The version each was on beforehand, keyed by name.
        post: The re-scanned installed set, keyed by name.

    Returns:
        Tuple of (upgraded, still-current) packages. A name the re-scan no longer
        knows about is dropped from both.
    """
    upgraded: list[Package] = []
    current: list[Package] = []

    for name in touched:
        pkg = post.get(name)
        if pkg is None:
            continue

        if _active_version(pkg) != pre_versions.get(name):
            upgraded.append(pkg)

        else:
            current.append(pkg)

    return upgraded, current


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

    targets, satisfied = _filter_kind(targets, satisfied, kind)

    formula_names = [p.name for p in targets if p.kind == PackageKind.FORMULA]
    cask_names = [p.name for p in targets if p.kind == PackageKind.CASK]
    pre_versions: dict[str, str | None] = {
        p.name: _active_version(p) for p in (*targets, *satisfied)
    }

    if formula_names:
        await run_upgrade(
            formula_names,
            _old_kegs(targets),
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
    touched = formula_names + cask_names + [p.name for p in satisfied]
    upgraded, current = _classify_outcome(touched, pre_versions, post)

    return upgraded, current, advisories, failures
