"""Filesystem-derived installed-state scanner for Brewery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import orjson

from brewery.core.config import BreweryENV, ensure_cache_dir, get_brewery_env
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import PackageKind, split_keg_version
from brewery.core.shell import run_capture

log: BreweryLogger = get_logger(name=__name__)

_RECEIPT_NAME = "INSTALL_RECEIPT.json"
_CASK_METADATA_DIR = ".metadata"

_SIZE_CACHE_FILE = "keg_sizes.json"
_SIZE_CONCURRENCY = 5

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


@dataclass(slots=True)
class InstalledRecord:
    """Filesystem-derived view of a single installed package."""

    name: str
    kind: PackageKind
    version: str
    revision: int = 0
    version_scheme: int | None = None
    installed_on: datetime | None = None
    # Receipt flags, captured now for a future `leaves`/autoremove
    installed_on_request: bool = False
    installed_as_dependency: bool = False
    deps: list[str] = field(default_factory=list)
    head: bool = False
    tap: str | None = None
    path: str | None = None  # Absolute path to the active keg/caskroom version
    stale_versions: list[str] = field(default_factory=list)
    linked: bool = False
    pinned: bool = False
    used_by: list[str] = field(default_factory=list)  # Installed reverse-deps
    size_kb: int | None = None  # Filled by attach_sizes()


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
        version_dirs: list[Path] = [
            d for d in _children(token_dir) if d.name != _CASK_METADATA_DIR
        ]

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


def _epoch_dt(value: object) -> datetime | None:
    """Convert a receipt epoch value to a datetime, tolerating bad input.

    Args:
        value: Receipt epoch value to convert.

    Returns:
        Datetime corresponding to the receipt epoch value, or None if it cannot be converted.
    """
    try:
        return datetime.fromtimestamp(float(value))  # ty: ignore[invalid-argument-type]

    except (TypeError, ValueError, OSError):
        return None


_size_semaphore: asyncio.Semaphore | None = None


def _get_size_semaphore() -> asyncio.Semaphore:
    """Lazily create the size-check semaphore, bound to the running loop.

    Returns:
        A semaphore bounding concurrent `du` calls.
    """
    global _size_semaphore
    if _size_semaphore is None:
        _size_semaphore = asyncio.Semaphore(_SIZE_CONCURRENCY)

    return _size_semaphore


async def attach_sizes(
    records: list[InstalledRecord], cache_dir: Path | None = None
) -> None:
    """Fill `size_kb` on each record, reusing cached sizes by keg mtime.

    Args:
        records: Records to size (mutated in place), including casks.
        cache_dir: Directory for the size cache (defaults to the brewery cache directory).
    """
    cache_dir = cache_dir or ensure_cache_dir()
    cached: dict[str, list] = _load_size_cache(cache_dir)

    sizes: list[tuple[int | None, int | None]] = await asyncio.gather(
        *(_resolve_size(record=r, cached=cached) for r in records)
    )

    fresh: dict[str, list[int]] = {}
    hits = 0
    for record, (size_kb, mtime_ns) in zip(records, sizes):
        record.size_kb = size_kb

        if size_kb is not None and mtime_ns is not None:
            fresh[record.name] = [mtime_ns, size_kb]
            entry = cached.get(record.name)

            if entry is not None and entry[0] == mtime_ns:
                hits += 1

    _save_size_cache(cache_dir=cache_dir, data=fresh)
    log.info(event="sizes_attached", total=len(records), cache_hits=hits)


async def _resolve_size(
    record: InstalledRecord, cached: dict[str, list]
) -> tuple[int | None, int | None]:
    """Return `(size_kb, mtime_ns)` for a record, reusing the cache on a hit.

    Args:
        record: The record to size.
        cached: The loaded size cache.

    Returns:
        A tuple of the size in KB (or None) and the keg mtime in ns (or None).
    """
    if record.path is None:
        return None, None

    keg = Path(record.path)
    mtime_ns: int | None = _safe_mtime_ns(keg)
    if mtime_ns is None:
        return None, None

    entry = cached.get(record.name)
    if entry is not None and entry[0] == mtime_ns:
        return entry[1], mtime_ns  # Unchanged keg: reuse cached size

    return await _du_kb(keg), mtime_ns


async def _du_kb(path: Path) -> int | None:
    """Measure a path's disk usage in KB via ``du -sk``, under the semaphore.

    Args:
        path: The path to measure.

    Returns:
        The disk usage in KB, or `None` if the measurement failed.
    """
    async with _get_size_semaphore():
        try:
            out, err, code = await run_capture("du", "-sk", str(object=path))

        except Exception as e:  # Subprocess spawn/timeout failures
            log.warning(
                event="keg_size_error", path=str(object=path), error=str(object=e)
            )
            return None

    if code != 0:
        log.warning(
            event="keg_size_failed",
            path=str(object=path),
            returncode=code,
            stderr=err,
        )
        return None

    try:
        return int(out.split()[0])

    except (ValueError, IndexError):
        log.warning(
            event="keg_size_parse_error", path=str(object=path), output=out[:80]
        )
        return None


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


def _record_to_cache_dict(record: InstalledRecord) -> dict:
    """Serialise an InstalledRecord to a JSON-safe dict for the record cache.

    Args:
        record: The InstalledRecord to serialise.

    Returns:
        A JSON-safe dict representing the record.
    """
    return {
        "name": record.name,
        "kind": record.kind.value,
        "version": record.version,
        "revision": record.revision,
        "version_scheme": record.version_scheme,
        "installed_on": record.installed_on.isoformat()
        if record.installed_on
        else None,
        "installed_on_request": record.installed_on_request,
        "installed_as_dependency": record.installed_as_dependency,
        "deps": record.deps,
        "head": record.head,
        "tap": record.tap,
        "path": record.path,
        "stale_versions": record.stale_versions,
        "linked": record.linked,
        "pinned": record.pinned,
        "used_by": record.used_by,
        "size_kb": record.size_kb,
    }


def _record_from_cache_dict(data: dict) -> InstalledRecord:
    """Rebuild an InstalledRecord from its cached dict.

    Args:
        data: The cached dict representing the record.

    Returns:
        The rebuilt InstalledRecord.
    """
    installed_on = data.get("installed_on")

    return InstalledRecord(
        name=data["name"],
        kind=PackageKind(data["kind"]),
        version=data["version"],
        revision=data.get("revision", 0),
        version_scheme=data.get("version_scheme"),
        installed_on=datetime.fromisoformat(installed_on) if installed_on else None,
        installed_on_request=data.get("installed_on_request", False),
        installed_as_dependency=data.get("installed_as_dependency", False),
        deps=data.get("deps", []),
        head=data.get("head", False),
        tap=data.get("tap"),
        path=data.get("path"),
        stale_versions=data.get("stale_versions", []),
        linked=data.get("linked", False),
        pinned=data.get("pinned", False),
        used_by=data.get("used_by", []),
        size_kb=data.get("size_kb"),
    )


# Public serialisation API for the FS-record cache
records_to_cache = _record_to_cache_dict
record_from_cache = _record_from_cache_dict
