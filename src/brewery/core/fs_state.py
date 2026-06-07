"""Filesystem-derived installed-state scanner for Brewery."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

import orjson

from brewery.core.config import BreweryENV, ensure_cache_dir, get_brewery_env
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import InstalledRecord, PackageKind, split_keg_version

log: BreweryLogger = get_logger(name=__name__)

_RECEIPT_NAME = "INSTALL_RECEIPT.json"
_CASK_METADATA_DIR = ".metadata"

_SIZE_CACHE_FILE = "keg_sizes.json"
_DU_BATCH = 256
_DU_TIMEOUT = 30

# Directories where linked kegs/casks are stored (current + legacy)
_LINKED_DIRS: tuple[Path, ...] = (
    Path("var/homebrew/linked"),
    Path("Library/LinkedKegs"),
)
_PINNED_DIRS: tuple[Path, ...] = (
    Path("var/homebrew/pinned"),
    Path("Library/PinnedKegs"),
)

# Subdirectories to probe for linked keg/cask executables (used as fallback)
_LINK_PROBE_SUBDIRS: tuple[str, ...] = ("bin", "sbin")


def scan_installed(env: BreweryENV | None = None) -> list[InstalledRecord]:
    """Scan the Cellar and Caskroom into a list of installed-state records.

    Args:
        env: Brewery environment to scan. Defaults to the discovered environment.

    Returns:
        One InstalledRecord per installed formula and cask.
    """
    env = env or get_brewery_env()

    formulae: list[InstalledRecord] = _scan_formulae(env)
    _apply_link_pin_state(records=formulae, env=env)

    records: list[InstalledRecord] = [*formulae, *_scan_casks(env)]
    _apply_reverse_deps(records=records)
    log.info(event="fs_scan_complete", count=len(records))

    return records


def _scan_formulae(env: BreweryENV) -> list[InstalledRecord]:
    """Build records for every installed formula under the Cellar.

    Args:
        env: Brewery environment to scan.

    Returns:
        One InstalledRecord per installed formula.
    """
    records: list[InstalledRecord] = []
    for formula_dir in _children(env.cellar):
        name: str = formula_dir.name
        active: Path | None = _active_keg(
            name=name, cellar=env.cellar, prefix=env.prefix
        )

        if active is None:
            log.warning(event="formula_no_active_keg", name=name)
            continue

        stale: list[str] = [d.name for d in _children(formula_dir) if d != active]
        receipt: dict | None = _read_receipt(active / _RECEIPT_NAME)
        records.append(
            _formula_record(name=name, active=active, receipt=receipt, stale=stale)
        )

    return records


def _scan_casks(env: BreweryENV) -> list[InstalledRecord]:
    """Build records for every installed cask under the Caskroom.

    Casks have no install receipt or dependency data, so the version comes
    from the Caskroom directory name and the install time from its mtime.

    Args:
        env: Brewery environment to scan.

    Returns:
        One InstalledRecord per installed cask.
    """
    records: list[InstalledRecord] = []
    for token_dir in _children(env.caskroom):
        # `.metadata` is excluded by _children (hidden), so any remaining child is a version directory
        version_dirs = _children(token_dir)

        if not version_dirs:
            log.warning(event="cask_no_version_dir", token=token_dir.name)
            continue

        active: Path = max(version_dirs, key=lambda d: _safe_mtime_ns(d) or 0)
        stale: list[str] = [d.name for d in version_dirs if d != active]
        records.append(
            InstalledRecord(
                name=token_dir.name,
                kind=PackageKind.CASK,
                version=active.name,
                installed_on=_mtime_dt(active),
                path=str(object=active),
                stale_versions=stale,
            )
        )

    return records


def _apply_link_pin_state(records: list[InstalledRecord], env: BreweryENV) -> None:
    """Populate `linked` and `pinned` boolean flags on a list of formula records in place.

    Linked state prefers the bookkeeping directory but falls back to the path-independent
    cross-check when directory is missing.

    Args:
        records: Formula records to enrich (mutated in place).
        env: Brewery environment.
    """
    linked: set[str] | None = linked_names(env.prefix)
    pinned: set[str] = pinned_names(env.prefix)

    use_fallback: bool = linked is None
    if use_fallback:
        log.warning(event="linked_dir_absent_using_fallback")

    for record in records:
        record.pinned = record.name in pinned
        record.linked = (
            is_effectively_linked(name=record.name, env=env)
            if use_fallback
            else record.name in linked
        )


def _apply_reverse_deps(records: list[InstalledRecord]) -> None:
    """Populate `used_by` on each record from the installed dependency graph.

    Args:
        records: All scanned records (mutated in place).
    """
    used_by: dict[str, list[str]] = {r.name: [] for r in records}
    for record in records:
        for dep in record.deps:
            key: str = dep if dep in used_by else dep.rsplit("/", 1)[-1]
            if key in used_by:
                used_by[key].append(record.name)

    for record in records:
        record.used_by = sorted(used_by[record.name])


def linked_names(prefix: Path) -> set[str] | None:
    """Return the set of linked formula names from brew's bookkeeping.

    Args:
        prefix: The Homebrew prefix.

    Returns:
        The set of linked names, or None if no bookkeeping directory exists.
    """
    return _bookkeeping_names(prefix=prefix, candidates=_LINKED_DIRS)


def pinned_names(prefix: Path) -> set[str]:
    """Return the set of pinned formula names from brew's bookkeeping.

    Args:
        prefix: The Homebrew prefix.

    Returns:
        The set of pinned names (empty when no bookkeeping directory exists).
    """
    return _bookkeeping_names(prefix=prefix, candidates=_PINNED_DIRS) or set()


def _bookkeeping_names(prefix: Path, candidates: tuple[Path, ...]) -> set[str] | None:
    """Read formula names from the first existing bookkeeping directory.

    Args:
        prefix: The Homebrew prefix.
        candidates: Relative directories to try in order of preference.

    Returns:
        The set of entry names, or None if none of the candidates exist.
    """
    for rel in candidates:
        directory: Path = prefix / rel
        if directory.is_dir():
            try:
                return {
                    p.name for p in directory.iterdir() if not p.name.startswith(".")
                }

            except OSError as e:
                log.warning(
                    event="bookkeeping_dir_unreadable",
                    path=str(object=directory),
                    error=str(object=e),
                )
                return None

    return None


def is_effectively_linked(name: str, env: BreweryENV) -> bool:
    """Path-independent fallback check for whether a formula is linked.

    A formula is effectively linked if its `opt` keg resolves and at least one
    executable in the prefix's `bin`/`sbin` is a symlink into that keg.

    Args:
        name: Formula name.
        env: Brewery environment.

    Returns:
        True if the formula appears linked by this heuristic.
    """
    opt: Path = env.prefix / "opt" / name
    try:
        keg: Path = opt.resolve(strict=True)

    except (OSError, RuntimeError):
        return False

    if not keg.is_dir():
        return False

    for subdir in _LINK_PROBE_SUBDIRS:
        probe: Path = env.prefix / subdir
        try:
            entries = list(probe.iterdir())

        except (FileNotFoundError, NotADirectoryError):
            continue

        for entry in entries:
            if not entry.is_symlink():
                continue

            try:
                target: Path = entry.resolve()

            except (OSError, RuntimeError):
                continue

            if keg == target or keg in target.parents:
                return True

    return False


def _active_keg(name: str, cellar: Path, prefix: Path) -> Path | None:
    """Resolve a formula's active keg directory.

    Falls back to the most recently modified version directory if the opt link is missing or broken.

    Args:
        name: Formula name.
        cellar: The Cellar root.
        prefix: The Homebrew prefix.

    Returns:
        The active keg directory, or None if the formula has no usable keg.
    """
    opt: Path = prefix / "opt" / name
    try:
        active: Path = opt.resolve(strict=True)
        if active.is_dir() and active.parent.name == name:
            return active

    except (OSError, RuntimeError):
        # Broken symlink, loop, or missing target: fall through to the scan
        pass

    candidates: list[Path] = _children(cellar / name)
    if not candidates:
        return None

    return max(candidates, key=lambda d: _safe_mtime_ns(d) or 0)


def _formula_record(
    name: str, active: Path, receipt: dict | None, stale: list[str]
) -> InstalledRecord:
    """Map an active keg (and its receipt, if any) to an InstalledRecord.

    Args:
        name: Package name
        active: Active keg directory
        receipt: Install receipt data, if available
        stale: List of stale version directories

    Returns:
        InstalledRecord for the active keg
    """
    version, revision = split_keg_version(active.name)

    # If no receipt, fall back to the keg mtime for the install date and skip flags
    if receipt is None:
        log.warning(event="receipt_missing", name=name, path=str(object=active))
        return InstalledRecord(
            name=name,
            kind=PackageKind.FORMULA,
            version=version,
            revision=revision,
            installed_on=_mtime_dt(active),
            path=str(object=active),
            stale_versions=stale,
        )

    source: dict = receipt.get("source") or {}
    runtime: list = receipt.get("runtime_dependencies") or []
    deps: list[str] = [d["full_name"] for d in runtime if d.get("full_name")]

    versions_meta: dict = source.get("versions") or {}
    version_scheme = versions_meta.get("version_scheme")

    raw_time = receipt.get("time")
    installed_on: datetime | None = (
        _epoch_dt(raw_time) if raw_time is not None else _mtime_dt(active)
    )

    return InstalledRecord(
        name=name,
        kind=PackageKind.FORMULA,
        version=version,
        revision=revision,
        version_scheme=version_scheme,
        installed_on=installed_on,
        installed_on_request=bool(receipt.get("installed_on_request", False)),
        installed_as_dependency=bool(receipt.get("installed_as_dependency", False)),
        deps=deps,
        head=source.get("spec") == "head",
        tap=source.get("tap"),
        path=str(object=active),
        stale_versions=stale,
    )


def _read_receipt(path: Path) -> dict | None:
    """Read and parse an INSTALL_RECEIPT.json, tolerating absence/corruption.

    Args:
        path: Path to the receipt file.

    Returns:
        The parsed receipt dict, or None if missing or unreadable.
    """
    try:
        data = orjson.loads(path.read_bytes())

    except FileNotFoundError:
        return None

    except (OSError, orjson.JSONDecodeError) as e:
        log.warning(
            event="receipt_unreadable", path=str(object=path), error=str(object=e)
        )
        return None

    return data if isinstance(data, dict) else None


def _children(base: Path) -> list[Path]:
    """Return non-hidden subdirectories of `base` (empty if base is absent).

    Args:
        base: Base directory to scan for children.

    Returns:
        List of non-hidden subdirectories of `base`.
    """
    try:
        return [p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")]

    except (FileNotFoundError, NotADirectoryError):
        return []


def _mtime_dt(path: Path) -> datetime | None:
    """Return a path's mtime as a datetime, or None if it cannot be stat'd.

    Args:
        path: Path to stat.

    Returns:
        Path's mtime as a datetime, or None if it cannot be stat'd.
    """
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)

    except OSError:
        return None


def _epoch_dt(value: int | float | str | None) -> datetime | None:
    """Convert a receipt epoch value to a datetime, tolerating bad input.

    Args:
        value: Receipt epoch value to convert.

    Returns:
        Datetime corresponding to the receipt epoch value, or None if it cannot be converted.
    """
    try:
        if value is not None:
            return datetime.fromtimestamp(float(value))

    except (TypeError, ValueError, OSError):
        return None


def attach_sizes(records: list[InstalledRecord], cache_dir: Path | None = None) -> None:
    """Fill `size_kb` on each record, reusing cached sizes by keg mtime.

    Args:
        records: Records to size (mutated in place), including casks.
        cache_dir: Directory for the size cache (defaults to the brewery cache directory).
    """
    cache_dir = cache_dir or ensure_cache_dir()
    cached: dict[str, list] = _load_size_cache(cache_dir)

    # Split into cache hits and the paths that still need measuring
    mtimes: dict[str, int] = {}  # record.name -> current keg mtime_ns
    misses: dict[str, Path] = {}  # keg path str -> Path, for the batched du
    miss_owner: dict[str, str] = {}  # keg path str -> record.name
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
            r.size_kb = entry[1]  # Unchanged: reuse cached size
            hits += 1

        else:
            misses[r.path] = keg
            miss_owner[r.path] = r.name

    measured: dict[str, int] = _du_many(list(misses.values()))
    by_name: dict[str, InstalledRecord] = {r.name: r for r in records}
    for path_str, size_kb in measured.items():
        by_name[miss_owner[path_str]].size_kb = size_kb

    # Rebuild the cache from current records (drops uninstalled packages)
    fresh: dict[str, list[int]] = {}
    for r in records:
        if r.size_kb is not None and r.name in mtimes:
            fresh[r.name] = [mtimes[r.name], r.size_kb]

    _save_size_cache(cache_dir=cache_dir, data=fresh)
    log.info(
        event="sizes_attached",
        total=len(records),
        cache_hits=hits,
        measured=len(misses),
    )


def _du_many(paths: list[Path]) -> dict[str, int]:
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
                ["du", "-sk", *(str(object=p) for p in batch)],
                capture_output=True,
                text=True,
                check=False,
                timeout=_DU_TIMEOUT,
            )

        except subprocess.TimeoutExpired:
            log.warning(event="keg_size_timeout", count=len(batch), timeout=_DU_TIMEOUT)
            continue

        except Exception as e:  # Subprocess spawn failures
            log.warning(event="keg_size_error", count=len(batch), error=str(object=e))
            continue

        out, err, code = proc.stdout, proc.stderr, proc.returncode

        if code != 0:
            # Parse partial results if du exits non-zero
            log.warning(event="keg_size_partial", returncode=code, stderr=err[:160])

        for line in out.splitlines():
            # Each line is "<kb>\t<path>"; path may contain spaces, so
            # split only on the first run of whitespace.
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

    Args:
        cache_dir: The directory containing the cache file.

    Returns:
        The loaded cache data, or an empty dictionary on error.
    """
    try:
        data = orjson.loads((cache_dir / _SIZE_CACHE_FILE).read_bytes())

    except (FileNotFoundError, orjson.JSONDecodeError, OSError):
        return {}

    return data if isinstance(data, dict) else {}


def _save_size_cache(cache_dir: Path, data: dict[str, list]) -> None:
    """Persist the size cache, swallowing write errors (sizing is best-effort).

    Args:
        cache_dir: The directory containing the cache file.
        data: The cache data to persist.
    """
    try:
        (cache_dir / _SIZE_CACHE_FILE).write_bytes(orjson.dumps(data))

    except OSError as e:
        log.warning(event="size_cache_write_failed", error=str(object=e))


def _safe_mtime_ns(path: Path) -> int | None:
    """Return a path's mtime in nanoseconds, or None if it cannot be stat'd.

    Args:
        path: The path to stat.

    Returns:
        The mtime in nanoseconds, or None on error.
    """
    try:
        return path.stat().st_mtime_ns

    except OSError:
        return None
