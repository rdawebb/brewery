"""Sweep stale kegs the retention policy no longer wants to keep."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.decorators import log_operation
from brewery.core.errors import OperationInProgressError
from brewery.core.locks import formula_lock
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import Notes, PackageKind
from brewery.core.repo import Repository
from brewery.core.settings import load_settings
from brewery.providers.cellar import rmtree
from brewery.providers.retention import CleanupCandidate, cleanup_candidates

log: BreweryLogger = get_logger(name=__name__)

# Racks swept concurrently, matching the download/install bounds
_CLEANUP_CONCURRENCY = 8


def remove_rack(
    name: str, kegs: list[CleanupCandidate], env: BreweryENV
) -> tuple[list[str], Notes]:
    """Delete every stale keg of one formula under a single rack lock.

    One lock acquisition per rack, not per keg: the rack lock is
    non-reentrant across threads, so two kegs of the same formula removed
    concurrently would make one of them look locked by a peer process.

    Args:
        name: The name of the formula.
        kegs: That formula's stale kegs, in selection order.
        env: The Brewery environment.

    Returns:
        Tuple of (removed "name version" strings, (label, reason) failures).

    Raises:
        OperationInProgressError: Another process holds the rack lock.
    """
    done: list[str] = []
    failed: Notes = []

    with formula_lock(name, prefix=env.prefix):
        for c in kegs:
            label = f"{c.name} {c.version}"
            try:
                rmtree(c.keg)
                done.append(label)

            except OSError as e:
                failed.append((label, str(e)))

    return done, failed


async def sweep_rack(
    name: str, kegs: list[CleanupCandidate], env: BreweryENV
) -> tuple[list[str], Notes]:
    """Remove one rack's stale kegs off the event loop, bounded by `sem`.

    Args:
        name: The name of the formula.
        kegs: That formula's stale kegs, in selection order.

    Returns:
        Tuple of (removed "name version" strings, (label, reason) failures).
    """
    sem = asyncio.Semaphore(_CLEANUP_CONCURRENCY)

    async with sem:
        try:
            return await asyncio.to_thread(remove_rack, name, kegs, env)

        except OperationInProgressError:
            # Mid-install process on this rack; the next sweep picks it up
            log.info(event="cleanup_skipped_locked", formula=name)

            return [], []


@log_operation(event_prefix="cleanup")
async def cleanup_packages(
    repo: Repository,
    max_age_days: int | None = None,
    *,
    env: BreweryENV | None = None,
) -> tuple[list[str], Notes]:
    """Remove stale kegs replaced more than max_age_days ago.

    Args:
        repo: The data facade to read installed state through.
        max_age_days: Age threshold in days, defaults to 30.
        env: Brewery environment (paths), resolved if omitted.

    Returns:
        Tuple of (removed "name version" strings, (label, reason) failures).
    """
    s = load_settings().retention
    age = s.age_days if max_age_days is None else max_age_days

    env = env or repo.cache_mgr.env or get_brewery_env()

    installed = repo.cache_mgr.installed_packages(kind=PackageKind.FORMULA)
    active = {Path(p.path) for p in installed if p.path}
    # Reuse the already-attached size cache so it never re-measures the active cellar
    active_sizes = {
        Path(p.path): p.size_kb for p in installed if p.path and p.size_kb is not None
    }

    by_rack: dict[str, list[CleanupCandidate]] = defaultdict(list)
    for c in cleanup_candidates(
        env.cellar,
        active=active,
        max_age_days=age,
        max_versions=s.max_versions,
        max_cellar_mb=s.max_cellar_mb,
        active_sizes=active_sizes,
    ):
        by_rack[c.name].append(c)

    results = await asyncio.gather(
        *(sweep_rack(name, kegs, env) for name, kegs in by_rack.items())
    )

    removed: list[str] = []
    failures: Notes = []
    # Flattened in submission order, so the summary stays deterministic
    for done, failed in results:
        removed += done
        failures += failed

    if removed:
        repo.cache_mgr.invalidate()

    return removed, failures
