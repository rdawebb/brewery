"""Run the external relocation tools, and lend files the write bit they need."""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg relocation logic.

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from brewery.core.errors import RelocationError

# Ad-hoc re-sign, preserving what install_name_tool would otherwise strip
_CODESIGN_ARGS = (
    "codesign",
    "--force",
    "--sign",
    "-",
    "--preserve-metadata=entitlements,flags,runtime",
)

# Per-call limits for batched codesign, kept well under ARG_MAX (argv+envp
# share ~1 MiB on macOS); a conservative budget leaves room for the environment.
_CODESIGN_ARG_BUDGET = 256 * 1024  # 256 KiB
_CODESIGN_MAX_ARGS = 4096  # 4 KiB


@dataclass(slots=True)
class _WritableHold:
    """One inode's borrowed write bit, and who is relying on it."""

    mode: int  # The mode to restore once the last holder leaves
    paths: set[str]  # Every name the hold was entered through
    depth: int = 1


# (st_dev, st_ino) -> hold, for the inodes `_writable` has made writable
_MADE_WRITABLE: dict[tuple[int, int], _WritableHold] = {}

_MODE_GUARD = threading.Lock()


def _run(cmd: list[str]) -> None:
    """Run install_name_tool / codesign synchronously.

    Args:
        cmd: The command to run.

    Raises:
        subprocess.CalledProcessError: If the command exits non-zero.
    """
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        check=False,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, proc.stdout, proc.stderr
        )


def _run_tool(argv: list[str], *, path: Path, tool: str, hint: str) -> None:
    """Run one relocation tool, mapping both failure modes to RelocationError.

    Args:
        argv: The full command line, tool name first.
        path: The file to report the failure against.
        tool: The tool's name, for the error message.
        hint: What to tell the user when the tool is not installed.

    Raises:
        RelocationError: If the tool exits non-zero or is not on PATH.
    """
    try:
        _run(argv)

    except subprocess.CalledProcessError as exc:
        raise RelocationError(path, f"{tool} failed: {exc.stderr.strip()}")

    except FileNotFoundError:
        raise RelocationError(path, f"{tool} not found on PATH; {hint}")


@contextlib.contextmanager
def _writable(path: Path | str) -> Iterator[None]:
    """Temporarily add the owner-write bit, restoring the original mode after.

    Permission bits belong to the inode, and bottles ship hard links, so holds
    are counted per inode under `_MODE_GUARD`: the bit goes on once and comes
    off when the last holder leaves.

    The restore is per name rather than per inode, because `codesign` replaces
    the file it signs; names that shared an inode can be separate files by the
    time the hold ends.

    Args:
        path: The path to the file to modify.
    """
    name = str(path)

    with _MODE_GUARD:
        st = os.stat(path)
        key = (st.st_dev, st.st_ino)
        held = _MADE_WRITABLE.get(key)
        if held is not None:
            held.depth += 1
            held.paths.add(name)

        elif not st.st_mode & 0o200:
            os.chmod(path, st.st_mode | 0o200)
            held = _MADE_WRITABLE[key] = _WritableHold(st.st_mode, {name})

    try:
        yield

    finally:
        if held is not None:
            with _MODE_GUARD:
                held.depth -= 1
                if held.depth == 0:
                    del _MADE_WRITABLE[key]
                    for restore in held.paths:
                        # A file may have been replaced, or not exist at all now
                        with contextlib.suppress(FileNotFoundError):
                            os.chmod(restore, held.mode)


def _chunk_paths(paths: list[Path], budget: int) -> list[list[Path]]:
    """Split paths into chunks whose combined byte length stays under `budget`.

    Keeps each codesign argv clear of ARG_MAX (MacOS caps argv+envp together);
    a single path longer than the budget still gets its own chunk.

    Args:
        paths: The paths to chunk.
        budget: The per-chunk byte budget for the path arguments.

    Returns:
        A list of non-empty path chunks, in input order.
    """
    chunks: list[list[Path]] = []
    current: list[Path] = []
    used = 0
    for path in paths:
        size = len(str(path).encode()) + 1  # +1 for the argv NUL terminator
        if current and (used + size > budget or len(current) >= _CODESIGN_MAX_ARGS):
            chunks.append(current)
            current = []
            used = 0

        current.append(path)
        used += size

    if current:
        chunks.append(current)

    return chunks


def _sign_order(paths: list[Path]) -> list[Path]:
    """Order a sign batch so nested code is signed before whatever contains it.

    Args:
        paths: The Mach-O files to re-sign.

    Returns:
        The same paths, deepest first, ties broken by path for a stable batch.
    """
    return sorted(paths, key=lambda p: (-len(p.parts), p.parts))


def _codesign(paths: list[Path]) -> None:
    """Ad-hoc re-sign a batch of Mach-O files.

    The batch is ordered by `_sign_order`, then chunked to stay under ARG_MAX;
    each file needs its owner-write bit for the in-place re-sign, so the whole
    chunk is made writable for the duration of the call.

    Args:
        paths: The Mach-O files to re-sign (may be empty).

    Raises:
        RelocationError: If codesign fails for any chunk.
    """
    for chunk in _chunk_paths(_sign_order(paths), _CODESIGN_ARG_BUDGET):
        with contextlib.ExitStack() as stack:
            for path in chunk:
                stack.enter_context(_writable(path))

            _run_tool(
                [*_CODESIGN_ARGS, *(str(p) for p in chunk)],
                path=chunk[0],
                tool="codesign",
                hint="install the Xcode Command Line Tools",
            )
