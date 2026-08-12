"""Restore a bottle's `etc`/`var` config into the prefix as real files.

A bottle carries the config the formula wrote into the prefix at build time under
`<keg>/.bottle/etc` and `<keg>/.bottle/var`. Those files are not linked from the
keg: they are copied in, so the user owns them from then on and an uninstall
leaves them behind.

The copy never clobbers an edited config. A destination that differs from the
bottled file is left alone and the new default lands beside it as `<name>.default`
-- unless the destination is itself an untouched default from an older keg of the
same formula, in which case it is advanced in place.
"""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's `Formula#install_etc_var` and
# the `InstallRenamed` destination policy.

from __future__ import annotations

import filecmp
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# The keg subdirectory holding bottled prefix config, and the roots within it
_BOTTLE_DIR = ".bottle"
_ROOTS = ("etc", "var")

_DEFAULT_SUFFIX = ".default"


@dataclass
class EtcVarResult:
    """Result of restoring one keg's bottled config."""

    copied: list[str] = field(default_factory=list)
    defaults: list[str] = field(default_factory=list)


def _identical(a: Path, b: Path) -> bool:
    """Report whether two files hold the same bytes.

    Args:
        a: The first file path.
        b: The second file path.

    Returns:
        True if both files exist and their contents match.
    """
    try:
        return filecmp.cmp(a, b, shallow=False)

    except OSError:
        return False


def _is_stale_default(dst: Path, keg: Path, rel: str) -> bool:
    """Report whether `dst` is an untouched default from an older keg.

    An unchanged config can be advanced in place from a previous keg; an edited
    config gains a neighbour `.default` file.

    Args:
        dst: The existing file in the prefix.
        keg: The keg being installed.
        rel: The path of the bottled file, relative to `<keg>/.bottle`.

    Returns:
        True if a sibling keg ships a `.bottle` copy identical to `dst`.
    """
    try:
        siblings = sorted(keg.parent.iterdir())

    except OSError:
        return False

    for sibling in siblings:
        if sibling == keg:
            continue

        candidate = sibling / _BOTTLE_DIR / rel
        if candidate.is_file() and _identical(dst, candidate):
            return True

    return False


def install_etc_var(keg: Path, *, prefix: Path) -> EtcVarResult:
    """Copy `<keg>/.bottle/{etc,var}` into `prefix` as real files.

    Directories are created with the default mode rather than the bottled one,
    and files carry their mode across but not their mtime.

    Args:
        keg: The installed keg directory.
        prefix: The Homebrew prefix to restore config into.

    Returns:
        The files copied and the `.default` files written.

    Raises:
        OSError: If a directory or file cannot be created in the prefix.
    """
    result = EtcVarResult()
    bottle = keg / _BOTTLE_DIR

    for root in _ROOTS:
        src_root = bottle / root
        if not src_root.is_dir():
            continue

        for dirpath, _, filenames in os.walk(src_root):
            src_dir = Path(dirpath)
            rel_dir = src_dir.relative_to(bottle)

            # Every directory is materialised, including the empty ones bottles
            # ship to reserve a cache or key directory
            dst_dir = prefix / rel_dir
            dst_dir.mkdir(parents=True, exist_ok=True)

            for filename in filenames:
                src = src_dir / filename
                dst = dst_dir / filename
                rel = str(rel_dir / filename)

                # A dir or symlink standing where the file belongs takes the copy as-is
                if dst.is_file():
                    if _identical(src, dst):
                        # Already the bottled default
                        continue

                    if not _is_stale_default(dst, keg, rel):
                        dst = dst.with_name(dst.name + _DEFAULT_SUFFIX)
                        shutil.copy(src, dst)
                        result.defaults.append(str(dst.relative_to(prefix)))

                        continue

                shutil.copy(src, dst)
                result.copied.append(rel)

    return result
