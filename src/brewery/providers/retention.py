"""Brewery-local keg retention metadata."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

REPLACED_SIDECAR = ".brewery_replaced.json"


@dataclass(frozen=True)
class CleanupCandidate:
    """A stale keg eligible for removal."""

    name: str
    version: str
    keg: Path
    replaced_at: int


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
    tmp.write_text(json.dumps(payload))
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
        return json.loads((keg / REPLACED_SIDECAR).read_text())

    except (OSError, ValueError):
        return None


def cleanup_candidates(
    cellar: Path, *, active: set[Path], max_age_days: int = 30, now: int | None = None
) -> list[CleanupCandidate]:
    """Stale kegs whose replaced_at is older than max_age_days.

    Sidecar-less stale kegs are excluded (not auto-eligible).

    Args:
        cellar: The Cellar directory.
        active: Active keg paths to never consider.
        max_age_days: Age threshold in days, defaults to 30.
        now: Unix epoch seconds, defaults to now.

    Returns:
        List of eligible candidates.
    """
    cutoff = (now if now is not None else int(time.time())) - max_age_days * 86400
    candidates: list[CleanupCandidate] = []

    try:
        with os.scandir(cellar) as cellar_entries:
            for name_entry in cellar_entries:
                if not name_entry.is_dir():
                    continue

                with os.scandir(name_entry.path) as keg_entries:
                    for keg_entry in keg_entries:
                        if not keg_entry.is_dir():
                            continue

                        keg_path = Path(keg_entry.path)

                        if keg_path in active:
                            continue

                        sidecar = read_replaced(keg_path)
                        if sidecar is None:
                            continue

                        at = sidecar.get("replaced_at")
                        if isinstance(at, int) and at <= cutoff:
                            candidates.append(
                                CleanupCandidate(
                                    name_entry.name, keg_entry.name, keg_path, at
                                )
                            )

    except FileNotFoundError:
        pass

    return candidates


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
