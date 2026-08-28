"""Helpers that stand in for a peer process holding one of brewery's locks.

A second `os.open` of the same lock file, in this process, contends exactly as
another process would: flock arbitrates between open file descriptions, not
between processes.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

from brewery.core.locks import lock_path

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def hold_lock(path: Path) -> int:
    """Take `path`'s lock from an unrelated fd and leave it held.

    For tests that release the descriptor themselves — from a timer, or partway
    through the body. Everything else wants `held_lock`.

    Args:
        path: The lock file to hold.

    Returns:
        The locked descriptor; close it to release.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)

    return fd


@contextmanager
def held_lock(path: Path) -> Iterator[int]:
    """Hold `path`'s lock from an unrelated fd for the duration of the block.

    Args:
        path: The lock file to hold.

    Yields:
        The locked descriptor, released on exit.
    """
    fd = hold_lock(path)
    try:
        yield fd

    finally:
        os.close(fd)


@contextmanager
def held_rack(prefix: Path, name: str) -> Iterator[int]:
    """Hold a formula's rack lock, as a peer mid-install would.

    Args:
        prefix: The Homebrew prefix.
        name: The formula whose rack to lock.

    Yields:
        The locked descriptor, released on exit.
    """
    with held_lock(lock_path(prefix, name)) as fd:
        yield fd


@contextmanager
def held_structure(prefix: Path) -> Iterator[int]:
    """Hold the prefix-wide structure lock, as a peer mid-link would.

    Args:
        prefix: The Homebrew prefix.

    Yields:
        The locked descriptor, released on exit.
    """
    with held_lock(lock_path(prefix, "brewery", kind="structure")) as fd:
        yield fd
