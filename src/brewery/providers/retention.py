"""Brewery-local keg retention metadata."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import orjson

from brewery.providers.receipt import read_receipt

REPLACED_SIDECAR = ".brewery_replaced.json"


@dataclass(frozen=True)
class CleanupCandidate:
    """A stale keg selected for removal."""

    name: str
    version: str
    keg: Path
    replaced_at: int | None  # Brew-upgraded kegs have no sidecar file
    reason: str = "aged"  # "aged" | "max_versions" | "max_cellar_mb"


@dataclass(frozen=True)
class _StaleKeg:
    """A non-active keg with the metadata the predicates rank on."""

    name: str
    version: str
    keg: Path
    replaced_at: int | None  # Sidecar supersession time, or None
    install_time: int  # Receipt 'time', else dir mtime fallback


def _install_time(keg: Path) -> int:
    """The keg's install time from its receipt, falling back to dir mtime.

    Used to order count/size eviction (keep newest-installed). Universal and
    read-only, so it works for sidecar-less and brew-installed kegs alike.

    Args:
        keg: The keg directory to get the install time for.

    Returns:
        The install time as an integer timestamp.
    """
    rec = read_receipt(keg)
    t = rec.get("time") if rec else None

    if isinstance(t, int):
        return t

    try:
        return int(keg.stat().st_mtime)

    except OSError:
        return 0


def _keg_size(keg: Path) -> int:
    """Total on-disk bytes of a keg (du-style; counts symlinks, not their targets).

    Args:
        keg: The keg directory to get the size for.

    Returns:
        The total on-disk bytes of the keg.
    """
    total = 0
    stack = [str(keg)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)

                        else:
                            total += entry.stat(follow_symlinks=False).st_size

                    except OSError:
                        pass

        except OSError:
            pass

    return total


def _scan_stale(
    cellar: Path, active: set[Path], *, need_install_time: bool
) -> list[_StaleKeg]:
    """Every non-active keg under the Cellar, with its ranking metadata.

    Args:
        cellar: The Cellar directory to scan.
        active: A set of active keg paths to exclude from the scan.
        need_install_time: Whether to resolve each keg's install time. Only the
            count/size predicates rank on it, so the aged-only path skips the
            per-keg receipt read.

    Returns:
        A list of _StaleKeg instances, one for each non-active keg.
    """
    out: list[_StaleKeg] = []
    try:
        with os.scandir(cellar) as name_entries:
            for name_entry in name_entries:
                if not name_entry.is_dir():
                    continue

                with os.scandir(name_entry.path) as keg_entries:
                    for keg_entry in keg_entries:
                        if not keg_entry.is_dir():
                            continue

                        keg = Path(keg_entry.path)
                        if keg in active:
                            continue

                        sidecar = read_replaced(keg)
                        at = sidecar.get("replaced_at") if sidecar else None
                        out.append(
                            _StaleKeg(
                                name=name_entry.name,
                                version=keg_entry.name,
                                keg=keg,
                                replaced_at=at if isinstance(at, int) else None,
                                install_time=_install_time(keg)
                                if need_install_time
                                else 0,
                            )
                        )

    except FileNotFoundError:
        pass

    return out


def mark_replaced(keg: Path, *, by: str | None = None, at: int | None = None) -> None:
    """Stamp a superseded keg with the time it was replaced.

    Written at keg root, sharing the keg's lifecycle. Records only the
    supersession time (and optionally the replacing version). Source of
    truth for cleanup retention.

    Args:
        keg: The old keg now retained as a stale version.
        by: Optional version that replaced it (for rollback).
        at: Unix epoch seconds, defaults to now.
    """
    payload: dict = {"replaced_at": at if at is not None else int(time.time())}
    if by is not None:
        payload["replaced_by"] = by

    sidecar = keg / REPLACED_SIDECAR
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    tmp.write_bytes(orjson.dumps(payload))
    os.replace(tmp, sidecar)


def read_replaced(keg: Path) -> dict | None:
    """Read a keg's replaced sidecar, tolerating absence/corruption.

    Args:
        keg: The keg directory.

    Returns:
        The parsed sidecar, or None if missing/unreadable — cleanup treats a
        missing sidecar as not auto-eligible.
    """
    try:
        return orjson.loads((keg / REPLACED_SIDECAR).read_bytes())

    except (OSError, ValueError):
        return None


def cleanup_candidates(
    cellar: Path,
    *,
    active: set[Path],
    max_age_days: int = 30,
    max_versions: int | None = None,
    max_cellar_mb: int | None = None,
    now: int | None = None,
) -> list[CleanupCandidate]:
    """Stale kegs whose replaced_at is older than max_age_days.

    Sidecar-less stale kegs are excluded (not auto-eligible).

    Args:
        cellar: The Cellar directory.
        active: Active keg paths to never consider.
        max_age_days: Age threshold in days, defaults to 30.
        max_versions: Maximum number of versions to retain, defaults to None.
        max_cellar_mb: Maximum total cellar size in MB, defaults to None.
        now: Unix epoch seconds, defaults to now.

    Returns:
        List of eligible candidates, each with the predicate that selected it.
    """
    at = now if now is not None else int(time.time())
    need_install_time = max_versions is not None or max_cellar_mb is not None
    stale = _scan_stale(cellar, active, need_install_time=need_install_time)
    candidates: dict[Path, str] = {}

    cutoff = at - max_age_days * 86400
    for sk in stale:
        if sk.replaced_at is not None and sk.replaced_at <= cutoff:
            candidates.setdefault(sk.keg, "aged")

    if max_versions is not None:
        by_name: dict[str, list[_StaleKeg]] = defaultdict(list)
        for sk in stale:
            by_name[sk.name].append(sk)

        for name, kegs in by_name.items():
            n_active = sum(1 for a in active if a.parent.name == name)
            keep = max(max_versions - n_active, 0)
            newest_first = sorted(kegs, key=lambda s: s.install_time, reverse=True)

            for sk in newest_first[keep:]:
                candidates.setdefault(sk.keg, "max_versions")

    if max_cellar_mb is not None:
        budget = max_cellar_mb * 1024 * 1024
        survivors = [sk for sk in stale if sk.keg not in candidates]
        sizes = {sk.keg: _keg_size(sk.keg) for sk in survivors}
        remaining = sum(_keg_size(a) for a in active) + sum(sizes.values())

        if remaining > budget:
            for sk in sorted(survivors, key=lambda s: s.install_time):  # oldest first
                if remaining <= budget:
                    break

                candidates[sk.keg] = "max_cellar_mb"
                remaining -= sizes[sk.keg]

    index = {sk.keg: sk for sk in stale}

    return [
        CleanupCandidate(
            name=index[keg].name,
            version=index[keg].version,
            keg=keg,
            replaced_at=index[keg].replaced_at,
            reason=reason,
        )
        for keg, reason in candidates.items()
    ]


def _stamp_path(cache_dir: Path) -> Path:
    """Fetch the path to the Brewery cleanup stamp file.

    Args:
        cache_dir: Brewery cache directory holding the last-run stamp.

    Returns:
        Path to the stamp file.
    """
    return cache_dir / ".brewery_cleanup_stamp"


def due_for_cleanup(
    cache_dir: Path, *, interval_days: int = 1, now: int | None = None
) -> bool:
    """Whether a cleanup sweep is due (last run older than interval, or never).

    Args:
        cache_dir: Brewery cache directory holding the last-run stamp.
        interval_days: Minimum days between sweeps.
        now: Unix epoch seconds; defaults to now.

    Returns:
        True if no stamp exists or it predates the interval.
    """
    at = now if now is not None else int(time.time())
    try:
        last = int(_stamp_path(cache_dir).read_text())

    except (OSError, ValueError):
        return True

    return at - last >= interval_days * 86400


def mark_cleanup_run(cache_dir: Path, *, at: int | None = None) -> None:
    """Record that a cleanup sweep just ran.

    Args:
        cache_dir: Brewery cache directory.
        at: Unix epoch seconds; defaults to now.
    """
    stamp = _stamp_path(cache_dir)
    tmp = stamp.with_name(stamp.name + ".tmp")
    tmp.write_text(str(at if at is not None else int(time.time())))
    os.replace(tmp, stamp)
