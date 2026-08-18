"""Pin and unpin formulae by writing brew's pinned-keg bookkeeping records."""

from __future__ import annotations

from pathlib import Path

from brewery.core.fs_state import PINNED_DIRS
from brewery.core.logging import BreweryLogger, get_logger
from brewery.providers.linker import make_relative_symlink

log: BreweryLogger = get_logger(name=__name__)


def pin_path(prefix: Path, name: str) -> Path:
    """Return the pinned-keg record path for a formula.

    Args:
        prefix: The Homebrew prefix.
        name: The formula name.

    Returns:
        `<prefix>/var/homebrew/pinned/<name>`.
    """
    return prefix / PINNED_DIRS[0] / name


def is_pinned(prefix: Path, name: str) -> bool:
    """Report whether a formula has a pinned-keg record.

    Args:
        prefix: The Homebrew prefix.
        name: The formula name.

    Returns:
        True if the record exists.
    """
    return pin_path(prefix=prefix, name=name).is_symlink()


def pin(prefix: Path, name: str, keg: Path) -> bool:
    """Pin a formula to a keg, brew-style, by symlinking the pinned-keg record.

    Brew pins the newest installed keg; brewery pins the keg the caller resolved
    as active (opt-first). These differ only when a stale newer keg survives a
    downgrade.

    Args:
        prefix: The Homebrew prefix.
        name: The formula name.
        keg: The keg directory to pin the formula at.

    Returns:
        True if the record was written, False if the formula was already pinned.
    """
    if is_pinned(prefix=prefix, name=name):
        return False

    make_relative_symlink(pin_path(prefix=prefix, name=name), keg)
    log.info(event="formula_pinned", name=name, keg=str(keg))

    return True


def unpin(prefix: Path, name: str) -> bool:
    """Unpin a formula by removing its pinned-keg record.

    The pinned directory is deliberately left in place once empty: it reads as an
    empty set, and keeping it present keeps the cache token's stat set stable.

    Args:
        prefix: The Homebrew prefix.
        name: The formula name.

    Returns:
        True if the record was removed, False if the formula was not pinned.
    """
    record: Path = pin_path(prefix=prefix, name=name)
    if not record.is_symlink():
        return False

    record.unlink()
    log.info(event="formula_unpinned", name=name)

    return True
