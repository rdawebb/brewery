"""Unit tests for the brew-compatible cross-process locks."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from brewery.core import locks
from brewery.core.errors import OperationInProgressError
from brewery.core.locks import (
    file_lock,
    formula_lock,
    lock_path,
    locks_dir,
    structure_lock,
)

# Reports whether an unrelated open file description can take the lock
_PROBE = """
import fcntl, os, sys

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("blocked")
else:
    print("acquired")
"""


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """Drop the process-local lock registry between tests."""
    yield

    locks._REGISTRY.clear()


def _hold(path: Path) -> int:
    """Take the lock from an unrelated fd, standing in for a peer process.

    Args:
        path: The lock file to hold.

    Returns:
        The locked descriptor; close it to release.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)

    return fd


def _probe(path: Path) -> bool:
    """Try to lock `path` from a separate fd, as a peer process would.

    Args:
        path: The lock file to probe.

    Returns:
        True if the lock was free.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    except BlockingIOError:
        return False

    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True

    finally:
        os.close(fd)


class TestLockPaths:
    """The paths must match brew's, or the locks do not interoperate."""

    def test_formula_lock_path_matches_brew(self, tmp_path) -> None:
        """`<prefix>/var/homebrew/locks/<rack>.formula.lock`, as LockFile builds it."""
        assert lock_path(tmp_path, "jq") == (
            tmp_path / "var" / "homebrew" / "locks" / "jq.formula.lock"
        )

    def test_locks_dir_is_brew_homebrew_locks(self, tmp_path) -> None:
        """The directory is brew's HOMEBREW_LOCKS, relative to the prefix."""
        assert locks_dir(tmp_path) == tmp_path / "var" / "homebrew" / "locks"

    def test_structure_lock_does_not_collide_with_a_formula(self, tmp_path) -> None:
        """A formula named `brewery` gets a different file from the structure lock.

        Both would otherwise be `brewery.lock`; the type suffix separates them.
        """
        with formula_lock("brewery", prefix=tmp_path), structure_lock(tmp_path):
            pass

        names = sorted(p.name for p in locks_dir(tmp_path).iterdir())

        assert names == ["brewery.formula.lock", "brewery.structure.lock"]


class TestAcquisition:
    """Taking, holding and releasing a lock."""

    def test_creates_the_lock_dir_and_file(self, tmp_path) -> None:
        """A first run has no locks directory; acquiring makes one."""
        assert not locks_dir(tmp_path).exists()

        with formula_lock("jq", prefix=tmp_path):
            assert lock_path(tmp_path, "jq").is_file()

    def test_lock_file_survives_release(self, tmp_path) -> None:
        """brew unlocks without unlinking (`unlink: false`) and so do we.

        Unlinking would reopen the inode race and break `brew cleanup`'s own
        pruning of unheld lock files.
        """
        with formula_lock("jq", prefix=tmp_path):
            pass

        assert lock_path(tmp_path, "jq").is_file()

    def test_released_lock_is_reacquirable(self, tmp_path) -> None:
        """The same lock can be taken again after release."""
        for _ in range(3):
            with formula_lock("jq", prefix=tmp_path):
                assert not _probe(lock_path(tmp_path, "jq"))

        assert _probe(lock_path(tmp_path, "jq"))

    def test_holds_against_another_descriptor(self, tmp_path) -> None:
        """While held, an unrelated fd cannot lock the file."""
        with formula_lock("jq", prefix=tmp_path):
            assert not _probe(lock_path(tmp_path, "jq"))

    def test_contention_names_the_rack(self, tmp_path) -> None:
        """The error points at the rack, as brew's message does, not the lock file."""
        path = lock_path(tmp_path, "jq")
        fd = _hold(path)
        try:
            with (
                pytest.raises(OperationInProgressError) as excinfo,
                formula_lock("jq", prefix=tmp_path),
            ):
                pass

        finally:
            os.close(fd)

        assert str(tmp_path / "Cellar" / "jq") in str(excinfo.value)
        assert excinfo.value.context["lock"] == path

    def test_timeout_waits_for_the_holder(self, tmp_path) -> None:
        """A bounded wait acquires the lock once the holder lets go."""
        path = lock_path(tmp_path, "jq")
        fd = _hold(path)

        threading.Timer(0.2, lambda: os.close(fd)).start()

        with file_lock(path, subject="jq", timeout=5.0):
            assert not _probe(path)

    def test_unwritable_lock_dir_raises_oserror(self, tmp_path) -> None:
        """A prefix we cannot write is an OSError, which callers already handle."""
        (tmp_path / "var").write_text("not a directory")

        with pytest.raises(OSError), formula_lock("jq", prefix=tmp_path):
            pass


class TestReentrancy:
    """`flock` is per open file description, so nesting needs explicit handling."""

    def test_nested_acquisition_of_the_same_lock(self, tmp_path) -> None:
        """The same thread can re-enter a lock it already holds."""
        with formula_lock("jq", prefix=tmp_path), formula_lock("jq", prefix=tmp_path):
            assert not _probe(lock_path(tmp_path, "jq"))

    def test_inner_exit_does_not_release(self, tmp_path) -> None:
        """Only the outermost exit drops the lock."""
        path = lock_path(tmp_path, "jq")
        with formula_lock("jq", prefix=tmp_path):
            with formula_lock("jq", prefix=tmp_path):
                pass

            assert not _probe(path)

        assert _probe(path)

    def test_different_locks_do_not_contend(self, tmp_path) -> None:
        """Two formulae hold their own racks simultaneously."""
        with formula_lock("jq", prefix=tmp_path), formula_lock("wget", prefix=tmp_path):
            assert not _probe(lock_path(tmp_path, "jq"))
            assert not _probe(lock_path(tmp_path, "wget"))


class TestThreads:
    """Two threads in one process must exclude each other, as two processes do."""

    def test_second_thread_is_refused(self, tmp_path) -> None:
        """A peer thread gets the contention error, not a false acquisition."""
        errors: list[Exception] = []

        def contend() -> None:
            """Try to take the lock the main thread is holding."""
            try:
                with formula_lock("jq", prefix=tmp_path):
                    pass

            except OperationInProgressError as exc:
                errors.append(exc)

        with formula_lock("jq", prefix=tmp_path):
            t = threading.Thread(target=contend)
            t.start()
            t.join()

        assert len(errors) == 1

    def test_second_thread_acquires_after_release(self, tmp_path) -> None:
        """The hand-off leaves the lock usable, i.e. the fd was really released."""
        held = threading.Event()
        acquired = threading.Event()

        def hold() -> None:
            """Hold the lock briefly, then release it."""
            with formula_lock("jq", prefix=tmp_path):
                held.set()
                time.sleep(0.1)

        t = threading.Thread(target=hold)
        t.start()
        held.wait(timeout=5)

        with formula_lock("jq", prefix=tmp_path, timeout=5.0):
            acquired.set()

        t.join()

        assert acquired.is_set()


class TestCrossProcess:
    """The property that actually matters: exclusion between processes."""

    def test_peer_process_is_blocked_then_admitted(self, tmp_path) -> None:
        """A child sees the lock held, and free again after release."""
        path = lock_path(tmp_path, "jq")

        with formula_lock("jq", prefix=tmp_path):
            assert _run_probe(path) == "blocked"

        assert _run_probe(path) == "acquired"


def _run_probe(path: Path) -> str:
    """Ask a child process whether it can take the lock.

    Args:
        path: The lock file to probe.

    Returns:
        `'blocked'` or `'acquired'`.
    """
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )

    return out.stdout.strip()


class TestInodeGuard:
    """brew re-checks the inode so it never holds a lock on an unlinked file."""

    def test_replaced_lock_file_is_retried(self, monkeypatch, tmp_path) -> None:
        """A single mismatch is retried, and the retry succeeds."""
        real = os.fstat
        seen = {"n": 0}

        def flaky(fd: int):
            """Report a mismatching inode on the first check only."""
            seen["n"] += 1
            if seen["n"] == 1:
                return types.SimpleNamespace(st_ino=-1)

            return real(fd)

        monkeypatch.setattr(locks.os, "fstat", flaky)

        with formula_lock("jq", prefix=tmp_path):
            pass

        assert seen["n"] == 2

    def test_endless_replacement_gives_up(self, monkeypatch, tmp_path) -> None:
        """A file that never settles is reported as contention, not held anyway."""
        monkeypatch.setattr(
            locks.os, "fstat", lambda _fd: types.SimpleNamespace(st_ino=-1)
        )

        with (
            pytest.raises(OperationInProgressError),
            formula_lock("jq", prefix=tmp_path),
        ):
            pass

    def test_unlinked_lock_file_is_retried(self, monkeypatch, tmp_path) -> None:
        """A lock file that has been deleted under us is not accepted."""
        path = lock_path(tmp_path, "jq")
        real = Path.stat
        seen = {"n": 0}

        def vanishing(self: Path, **kwargs):
            """Behave as though the lock file were unlinked on the first check."""
            if self == path:
                seen["n"] += 1
                if seen["n"] == 1:
                    raise FileNotFoundError(path)

            return real(self, **kwargs)

        monkeypatch.setattr(Path, "stat", vanishing)

        with formula_lock("jq", prefix=tmp_path):
            pass

        assert seen["n"] == 2


class TestStructureLock:
    """The prefix-wide lock guarding shared-directory ownership."""

    def test_waits_rather_than_failing(self, tmp_path) -> None:
        """Peers hold it briefly, so the default is a bounded wait."""
        path = lock_path(tmp_path, "brewery", kind="structure")
        fd = _hold(path)

        threading.Timer(0.2, lambda: os.close(fd)).start()

        with structure_lock(tmp_path):
            assert not _probe(path)

    def test_gives_up_after_its_timeout(self, tmp_path) -> None:
        """A holder that never lets go surfaces as contention."""
        path = lock_path(tmp_path, "brewery", kind="structure")
        fd = _hold(path)
        try:
            with (
                pytest.raises(OperationInProgressError),
                structure_lock(tmp_path, timeout=0.1),
            ):
                pass

        finally:
            os.close(fd)


class TestContentionLatch:
    """One waiter teaches the process; the rest probe instead of re-waiting.

    Without this, every thread queued behind `linker._STRUCTURE_LOCK` burns its
    own full `_STRUCTURE_TIMEOUT` against the same peer, so eight install threads
    take eight timeouts to report one contended lock.
    """

    def test_the_second_caller_does_not_wait_again(self, tmp_path) -> None:
        """A peer that is still holding fails the next caller straight away."""
        path = lock_path(tmp_path, "brewery", kind="structure")
        fd = _hold(path)
        try:
            with (
                pytest.raises(OperationInProgressError),
                structure_lock(tmp_path, timeout=0.5),
            ):
                pass

            start = time.monotonic()
            with (
                pytest.raises(OperationInProgressError),
                structure_lock(tmp_path, timeout=0.5),
            ):
                pass

            # Probe is one non-blocking flock, so this is under the 0.05s first poll
            assert time.monotonic() - start < 0.1

        finally:
            os.close(fd)

    def test_a_released_lock_is_still_acquired(self, tmp_path) -> None:
        """The latch never strands a lock the peer has since let go of."""
        path = lock_path(tmp_path, "brewery", kind="structure")
        fd = _hold(path)

        with (
            pytest.raises(OperationInProgressError),
            structure_lock(tmp_path, timeout=0.1),
        ):
            pass

        os.close(fd)

        with structure_lock(tmp_path, timeout=0.1):
            assert not _probe(path)

    def test_success_clears_the_latch(self, tmp_path) -> None:
        """A later peer gets the full budget rather than the stale answer."""
        path = lock_path(tmp_path, "brewery", kind="structure")
        fd = _hold(path)

        with (
            pytest.raises(OperationInProgressError),
            structure_lock(tmp_path, timeout=0.1),
        ):
            pass

        os.close(fd)

        with structure_lock(tmp_path, timeout=0.1):
            pass

        # Latched again, the next caller would fail fast; cleared, it waits out
        # the holder that goes away mid-wait
        fd = _hold(path)
        threading.Timer(0.2, lambda: os.close(fd)).start()

        with structure_lock(tmp_path, timeout=5.0):
            assert not _probe(path)

    def test_a_fail_fast_lock_never_short_circuits(self, tmp_path) -> None:
        """`formula_lock`'s zero timeout gives the latch no window to trust."""
        path = lock_path(tmp_path, "jq")
        fd = _hold(path)
        try:
            for _ in range(2):
                with (
                    pytest.raises(OperationInProgressError),
                    formula_lock("jq", prefix=tmp_path),
                ):
                    pass

        finally:
            os.close(fd)

        assert locks._entry(path).contended_at == 0.0

    def test_reentrancy_is_unaffected(self, tmp_path) -> None:
        """Nesting in one thread still re-enters on the existing descriptor."""
        path = lock_path(tmp_path, "brewery", kind="structure")

        with structure_lock(tmp_path), structure_lock(tmp_path):
            assert not _probe(path)

        assert _probe(path)
