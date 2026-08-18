"""Disk-usage measurement and size-cache management for installed kegs."""

from __future__ import annotations

import contextlib
import os
import subprocess
from pathlib import Path

import orjson

from brewery.core.config import ensure_cache_dir
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import InstalledRecord

log: BreweryLogger = get_logger(name=__name__)

_SIZE_CACHE_FILE = "keg_sizes.json"
_DU_BATCH = 256
_DU_TIMEOUT = 30


def attach_sizes(records: list[InstalledRecord], cache_dir: Path | None = None) -> None:
    """Fill `size_kb` on each record, reusing cached sizes by keg mtime.

    Args:
        records: Records to size (mutated in place), including casks.
        cache_dir: Directory for the size cache (defaults to the brewery cache directory).
    """
    from brewery.core.fs_state import _safe_mtime_ns

    cache_dir = cache_dir or ensure_cache_dir()
    cached: dict[str, list] = _load_size_cache(cache_dir)

    mtimes: dict[str, int] = {}
    misses: dict[str, Path] = {}
    miss_owner: dict[str, str] = {}
    hits = 0

    for r in records:
        if r.path is None:
            continue

        keg = Path(r.path)
        mtime_ns = _safe_mtime_ns(keg)
        if mtime_ns is None:
            continue

        mtimes[r.name] = mtime_ns
        entry = cached.get(r.name)
        if entry is not None and entry[0] == mtime_ns:
            r.size_kb = entry[1]
            hits += 1

        else:
            misses[r.path] = keg
            miss_owner[r.path] = r.name

    measured: dict[str, int] = du_many(list(misses.values()))
    by_name: dict[str, InstalledRecord] = {r.name: r for r in records}
    for path_str, size_kb in measured.items():
        by_name[miss_owner[path_str]].size_kb = size_kb

    for r in records:
        if r.size_kb is not None and r.name in mtimes:
            cached[r.name] = [mtimes[r.name], r.size_kb, r.path]

    _save_size_cache(cache_dir=cache_dir, data=_prune(cached, set(mtimes)))
    log.info(
        event="sizes_attached",
        total=len(records),
        cache_hits=hits,
        measured=len(misses),
    )


def _prune(cache: dict[str, list], sized: set[str]) -> dict[str, list]:
    """Drop the entries whose keg is no longer on disk.

    Args:
        cache: The merged cache to prune.
        sized: Names this call measured or confirmed.

    Returns:
        The entries worth keeping.
    """
    return {
        name: entry
        for name, entry in cache.items()
        if name in sized or (len(entry) > 2 and os.path.exists(entry[2]))
    }


def du_many(paths: list[Path]) -> dict[str, int]:
    """Measure several paths' disk usage in one (chunked) `du -sk` per batch.

    Args:
        paths: Keg/caskroom paths to size.

    Returns:
        Mapping of path string -> size in KB. Paths that `du` could not
        measure are absent from the result.
    """
    if not paths:
        return {}

    sizes: dict[str, int] = {}
    for i in range(0, len(paths), _DU_BATCH):
        batch: list[Path] = paths[i : i + _DU_BATCH]

        try:
            proc = subprocess.run(
                ["du", "-sk", *(str(p) for p in batch)],
                capture_output=True,
                text=True,
                check=False,
                timeout=_DU_TIMEOUT,
            )

        except subprocess.TimeoutExpired:
            log.warning(event="keg_size_timeout", count=len(batch), timeout=_DU_TIMEOUT)
            continue

        except OSError as e:
            log.warning(event="keg_size_error", count=len(batch), error=str(e))
            continue

        out, err, code = proc.stdout, proc.stderr, proc.returncode

        if code != 0:
            log.warning(event="keg_size_partial", returncode=code, stderr=err[:160])

        for line in out.splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue

            kb_str, path_str = parts
            try:
                sizes[path_str] = int(kb_str)

            except ValueError:
                log.warning(event="keg_size_parse_error", line=line[:80])

    return sizes


def _load_size_cache(cache_dir: Path) -> dict[str, list]:
    """Load the size cache, returning an empty map on any error.

    Entries that are not `[mtime_ns, size_kb, ...]` are discarded, so a hand-edited
    or half-written file cannot raise on lookup.

    Args:
        cache_dir: The directory containing the cache file.

    Returns:
        The loaded cache data, or an empty dictionary on error.
    """
    try:
        data = orjson.loads((cache_dir / _SIZE_CACHE_FILE).read_bytes())

    except (FileNotFoundError, orjson.JSONDecodeError, OSError):
        return {}

    if not isinstance(data, dict):
        return {}

    return {
        name: entry
        for name, entry in data.items()
        if isinstance(entry, list) and len(entry) >= 2
    }


def _save_size_cache(cache_dir: Path, data: dict[str, list]) -> None:
    """Persist the size cache, swallowing write errors (sizing is best-effort).

    Staged beside the cache and renamed over it: the refresh daemon and a command
    can be sizing at the same time, and a reader that caught the file mid-write
    would take it for corrupt and re-measure every keg.

    Args:
        cache_dir: The directory containing the cache file.
        data: The cache data to persist.
    """
    path = cache_dir / _SIZE_CACHE_FILE
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")

    try:
        tmp.write_bytes(orjson.dumps(data))
        os.replace(tmp, path)

    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink()

        log.warning(event="size_cache_write_failed", error=str(e))
