"""Filesystem-derived installed-state scanner for Brewery."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import PackageKind, split_keg_version

log: BreweryLogger = get_logger(name=__name__)

_RECEIPT_NAME = "INSTALL_RECEIPT.json"
_CASK_METADATA_DIR = ".metadata"

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

        active: Path = max(version_dirs, key=_safe_mtime)
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

    return max(candidates, key=_safe_mtime)


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

    # No receipt: API-loaded installs should always write one, but fall back to
    # the keg mtime for the install date and skip the provenance flags.
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

    raw_time = receipt.get("time")
    installed_on: datetime | None = (
        _epoch_dt(raw_time) if raw_time is not None else _mtime_dt(active)
    )

    return InstalledRecord(
        name=name,
        kind=PackageKind.FORMULA,
        version=version,
        revision=revision,
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
        data = json.loads(path.read_text())

    except FileNotFoundError:
        return None

    except (OSError, json.JSONDecodeError) as e:
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


def _safe_mtime(path: Path) -> float:
    """Return a path's mtime, or 0.0 if it cannot be stat'd.

    Args:
        path: Path to stat.

    Returns:
        Path's mtime, or 0.0 if it cannot be stat'd.
    """
    try:
        return path.stat().st_mtime

    except OSError:
        return 0.0


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
