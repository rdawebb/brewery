"""Cross-process advisory locks over the Homebrew prefix.

`formula_lock` is wire-compatible with brew's own per-rack lock, so a brewery
and a `brew` write operation of the same formula exclude each other.

`structure_lock` is brewery-only: it guards the shared-directory ownership
changes in `providers.linker`, which are per-prefix rather than per-rack and which
brew does not lock at all.
"""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's LockFile.

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from brewery.core.errors import OperationInProgressError
from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

# brew's HOMEBREW_LOCKS, relative to the prefix (startup/config.rb)
_LOCKS_SUBDIR = ("var", "homebrew", "locks")

# A lock file unlinked is retried, bounded (unlike brew's `retry`)
_MAX_INODE_RETRIES = 10

_POLL_MIN = 0.05
_POLL_MAX = 0.5

# How long the prefix-wide structure lock waits
_STRUCTURE_TIMEOUT = 30.0


def locks_dir(prefix: Path) -> Path:
    """The lock directory for a prefix.

    Args:
        prefix: The Homebrew prefix.

    Returns:
        `<prefix>/var/homebrew/locks`, which may not exist yet.
    """
    return prefix.joinpath(*_LOCKS_SUBDIR)


def lock_path(prefix: Path, name: str, *, kind: str = "formula") -> Path:
    """The lock file for one subject, in brew's naming scheme.

    Args:
        prefix: The Homebrew prefix.
        name: The lock name: brew uses the rack's basename, i.e. the formula name.
        kind: brew's lock type, which suffixes the filename.

    Returns:
        `<prefix>/var/homebrew/locks/<name>.<kind>.lock`.
    """
    return locks_dir(prefix) / f"{name}.{kind}.lock"


@dataclass
class _Held:
    """One process's hold on a lock file.

    `guard` gives in-process mutual exclusion and same-thread reentrancy, which
    `flock` alone cannot: locks belong to the open file description, so a second
    `os.open` of the same path in this process contends with the first exactly as
    another process would.
    """

    guard: threading.RLock = field(default_factory=threading.RLock)
    fd: int = -1
    depth: int = 0

    # `time.monotonic()` of the last acquisition that waited out its whole budget
    # and still lost, or 0.0 if the last attempt succeeded
    contended_at: float = 0.0


_REGISTRY: dict[Path, _Held] = {}
_REGISTRY_GUARD = threading.Lock()


def _entry(path: Path) -> _Held:
    """Get or create the process-local record for a lock file.

    Args:
        path: The lock file path.

    Returns:
        The `_Held` shared by every acquirer of `path` in this process.
    """
    with _REGISTRY_GUARD:
        return _REGISTRY.setdefault(path, _Held())


def _release(fd: int) -> None:
    """Unlock and close a lock file descriptor.

    Args:
        fd: The descriptor to release. Closing alone would release the `flock`;
            unlocking first keeps the sequence explicit.
    """
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)

    with contextlib.suppress(OSError):
        os.close(fd)


def _acquire_fd(
    path: Path, *, subject: str, deadline: float, held: _Held, grace: float
) -> int:
    """Open and `flock` a lock file, polling until `deadline`.

    If a previous caller in this process already waited out its whole budget on a
    peer, the poll loop is skipped: the first non-blocking attempt becomes a probe,
    and losing it fails immediately.

    Args:
        path: The lock file to create and lock.
        subject: What is being locked, for the error message.
        deadline: `time.monotonic()` value past which contention is fatal.
        held: This process's record for `path`, carrying the contention latch.
        grace: How long a recorded contention stays trusted; the caller's own timeout.

    Returns:
        A locked file descriptor, owned by the caller.

    Raises:
        OperationInProgressError: The lock is held elsewhere, or the file kept
            being replaced on disk.
        OSError: The lock directory or file could not be created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    delay = _POLL_MIN
    changed = 0
    probe_only = bool(held.contended_at) and (
        time.monotonic() - held.contended_at < grace
    )

    while True:
        # Python opens descriptors non-inheritable (PEP 446), which is brew's
        # explicit FD_CLOEXEC: a `brew` subprocess must not inherit this lock
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        except BlockingIOError:
            os.close(fd)
            now = time.monotonic()

            # The peer a previous caller lost to is still there; do not re-wait
            if probe_only:
                raise OperationInProgressError(subject, path=path) from None

            remaining = deadline - now
            if remaining <= 0:
                # Fail-fast caller found the lock busy; probe-only re-raises
                if grace > 0:
                    held.contended_at = now

                raise OperationInProgressError(subject, path=path) from None

            time.sleep(min(delay, remaining))
            delay = min(delay * 2, _POLL_MAX)
            continue

        except OSError:
            os.close(fd)
            raise

        # Locked file has been replaced; retry with a new FD
        try:
            if os.fstat(fd).st_ino == path.stat().st_ino:
                held.contended_at = 0.0

                return fd

        except FileNotFoundError:
            pass

        _release(fd)
        changed += 1
        if changed >= _MAX_INODE_RETRIES:
            log.warning(
                event="lock_file_kept_changing", lock=str(path), attempts=changed
            )

            raise OperationInProgressError(subject, path=path)


@contextlib.contextmanager
def file_lock(path: Path, *, subject: str, timeout: float = 0.0) -> Iterator[None]:
    """Hold an exclusive lock on `path` for the duration of the block.

    Reentrant within a thread; exclusive against other threads and processes.
    The lock file is left in place on release, and removed by `cleanup`.

    Once one caller has waited out `timeout` and lost to a peer process, callers
    that follow it within `timeout` probe once and fail immediately rather than
    each waiting again.

    Args:
        path: The lock file.
        subject: What is being locked, for the error message.
        timeout: Seconds to wait for the lock. The default of 0 fails
            immediately, which is brew's `LOCK_NB` behaviour.

    Yields:
        None, with the lock held.

    Raises:
        OperationInProgressError: The lock could not be taken within `timeout`.
    """
    deadline = time.monotonic() + timeout
    held = _entry(path)

    if not (
        held.guard.acquire(timeout=timeout)
        if timeout > 0
        else held.guard.acquire(blocking=False)
    ):
        raise OperationInProgressError(subject, path=path)

    try:
        if held.depth == 0:
            held.fd = _acquire_fd(
                path, subject=subject, deadline=deadline, held=held, grace=timeout
            )

        held.depth += 1

    except BaseException:
        held.guard.release()
        raise

    try:
        yield

    finally:
        held.depth -= 1
        if held.depth == 0:
            fd, held.fd = held.fd, -1
            _release(fd)

        held.guard.release()


def formula_lock(
    name: str, *, prefix: Path, timeout: float = 0.0
) -> AbstractContextManager[None]:
    """Lock one formula's rack, excluding both brewery and brew.

    Must not be held across a `brew` subprocess: brew locks the same rack, so it
    would fail with its own `OperationInProgressError`.

    Args:
        name: The formula (rack) name.
        prefix: The Homebrew prefix.
        timeout: Seconds to wait. The default fails fast, because the holder
            is doing a whole install.

    Returns:
        A context manager holding the rack lock.
    """
    return file_lock(
        lock_path(prefix, name),
        subject=str(prefix / "Cellar" / name),
        timeout=timeout,
    )


def structure_lock(
    prefix: Path, *, timeout: float | None = None
) -> AbstractContextManager[None]:
    """Lock shared-directory ownership changes across the whole prefix.

    Per-rack locks cannot serialise this, so this guards against two different
    formulae exploding the same `lib/pkgconfig` conflict while holding different
    rack locks.

    Args:
        prefix: The Homebrew prefix.
        timeout: Seconds to wait before giving up; defaults to `_STRUCTURE_TIMEOUT`.

    Returns:
        A context manager holding the prefix-wide structure lock.
    """
    return file_lock(
        lock_path(prefix, "brewery", kind="structure"),
        subject=f"the shared directories under {prefix}",
        timeout=_STRUCTURE_TIMEOUT if timeout is None else timeout,
    )
