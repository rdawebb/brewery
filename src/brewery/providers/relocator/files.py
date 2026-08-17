"""Classify one finished file and relocate it, on a bounded thread pool."""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg relocation logic.

from __future__ import annotations

import os
import struct
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import TypeVar

from brewery.core.errors import RelocationError

from .elf import (
    _ELF_MAGIC,
    _build_elf_args,
    _ElfInfo,
    _read_elf,
    _run_patchelf,
)
from .macho import (
    _MACHO_MAGICS,
    _collect_names,
    _NameSlot,
    _relocate_macho,
)
from .reader import _Reader
from .substitutions import (
    _PLACEHOLDER_MARKER,
    _PLACEHOLDER_MARKER_STR,
    _substitute,
)
from .tools import _writable

# Result of a tolerant binary-header parse, whatever shape that parser returns
_T = TypeVar("_T")

_AR_MAGIC = b"!<arch>\n"  # Static archive (ar) magic

# Bounded thread pool for the regular-file relocation phase
_RELOCATE_WORKERS = min(8, os.cpu_count() or 4)

# Bytes `_classify_head` needs: the longest magic number it tests for
_HEAD_BYTES = 8

# Read size for the marker scan over a file whose body has to be searched
_SCAN_CHUNK = 1024 * 1024  # 1 MiB


class _Kind(Enum):
    """Internal file classification for the fused relocation path."""

    MACHO = "macho"
    ELF = "elf"
    ARCHIVE = "archive"
    TEXT = "text"


def _classify(head: bytes) -> _Kind:
    """Classify a file from its first `_HEAD_BYTES` bytes.

    Called only after the size guard, so the file is at least
    `len(_PLACEHOLDER_MARKER)` (11) bytes & the magic reads are safe.

    Args:
        head: The file's first `_HEAD_BYTES` bytes.

    Returns:
        The file's classification.
    """
    if head == _AR_MAGIC:
        return _Kind.ARCHIVE

    if struct.unpack_from(">I", head, 0)[0] in _MACHO_MAGICS:
        return _Kind.MACHO

    if head[:4] == _ELF_MAGIC:
        return _Kind.ELF

    return _Kind.TEXT


def _try_parse(parse: Callable[[_Reader], _T], reader: _Reader, empty: _T) -> _T:
    """Parse a binary header, treating a malformed one as nothing to rewrite.

    A file whose magic says Mach-O or ELF but whose header will not parse is not
    a keg the relocator can fix, and it is not one it should abort over either:
    the linker and `codesign` will catch and report it.

    Args:
        parse: The header parser to run.
        reader: A reader positioned over the whole file.
        empty: What the parser would have returned had it found nothing.

    Returns:
        The parse result, or `empty` if the header could not be parsed.
    """
    try:
        return parse(reader)

    except (struct.error, ValueError):
        return empty


def _has_marker(fd: int, size: int) -> bool:
    """Whether the placeholder marker appears anywhere in a file's bytes.

    Scans in chunks rather than reading the file whole, so a large binary that
    reaches the text branch costs a bounded amount of memory.

    Args:
        fd: The file descriptor to scan.
        size: The file's size in bytes.

    Returns:
        True if the marker is present.
    """
    overlap = len(_PLACEHOLDER_MARKER) - 1
    off = 0
    tail = b""

    while off < size:
        chunk = os.pread(fd, _SCAN_CHUNK, off)
        if not chunk:
            return False

        if _PLACEHOLDER_MARKER in chunk:
            return True

        # A marker straddling the seam is only visible across both chunks
        if tail and _PLACEHOLDER_MARKER in tail + chunk[:overlap]:
            return True

        tail = chunk[-overlap:]
        off += len(chunk)

    return False


def _process_file(
    path_str: str,
    subs: dict[bytes, bytes],
    keg_root: str,
    allowed_text: frozenset[str] | None,
    skip_linkage: bool,
) -> tuple[Path | None, str | None, bool]:
    """Relocate one regular (non-symlink) file from a single open descriptor.

    A rewritten Mach-O has its install names fixed but is left unsigned; the
    returned path is handed back to `StreamRelocator.finish`, which batches the
    ad-hoc re-sign across the whole keg - an ELF is rewritten in place via
    patchelf and needs no re-signing, so it is only counted.

    Args:
        path_str: The path to the file (str; Path is deferred to here).
        subs: A mapping of placeholder bytes to their replacements.
        keg_root: The keg directory as a string, for computing relative paths.
        allowed_text: The manifest's changed_files set (relative POSIX), or None
            to substitute any marker-bearing text file.
        skip_linkage: When True, leave binary (Mach-O/ELF) dynamic linkage
            untouched; text substitution still runs.

    Returns:
        (macho_path, text_rel, elf_relocated): `macho_path` the file's path if a
        Mach-O was rewritten (and now needs re-signing), else None; `text_rel`
        the relative POSIX path if a text file was substituted, else None;
        `elf_relocated` True if an ELF's linkage was rewritten.

    Raises:
        RelocationError: If the file could not be relocated.
    """
    macho_slots: list[_NameSlot] | None = None
    elf_args: list[str] | None = None
    new_text: bytes | None = None
    text_rel: str | None = None

    try:
        with open(path_str, "rb") as fh:
            fd = fh.fileno()
            size = os.fstat(fd).st_size

            # Too short to hold a placeholder
            if size < len(_PLACEHOLDER_MARKER):
                return None, None, False

            reader = _Reader(fd, size)
            kind = _classify(reader.read(0, _HEAD_BYTES))

            if kind is _Kind.MACHO:
                if skip_linkage:
                    return None, None, False  # :any_skip_relocation

                slots = _try_parse(_collect_names, reader, [])
                if not any(_PLACEHOLDER_MARKER_STR in slot.value for slot in slots):
                    return None, None, False

                macho_slots = slots

            elif kind is _Kind.ELF:
                if skip_linkage:
                    return None, None, False  # :any_skip_relocation

                info = _try_parse(_read_elf, reader, _ElfInfo(None, None, None))
                if not any(
                    s is not None and _PLACEHOLDER_MARKER_STR in s
                    for s in (info.interp, info.rpath, info.runpath)
                ):
                    return None, None, False

                elf_args = _build_elf_args(Path(path_str), info, subs)

            elif kind is _Kind.ARCHIVE:
                # Length-changing text substitution would corrupt headers and offsets
                if _has_marker(fd, size):
                    raise RelocationError(
                        Path(path_str), "static archive contains a placeholder"
                    )

                return None, None, False

            else:
                rel = path_str[len(keg_root) + 1 :]
                # In manifest mode, only substitute files brew listed
                if allowed_text is not None and rel not in allowed_text:
                    return None, None, False

                # No full-file read for marker-free files
                if not _has_marker(fd, size):
                    return None, None, False

                new = _substitute(Path(path_str), fh.read(), subs)
                if new is not None:
                    new_text = new
                    text_rel = rel

    except OSError as exc:
        raise RelocationError(Path(path_str), f"read failed: {exc}") from exc

    # The descriptor is closed here, so it is safe to mutate the file
    path = Path(path_str)
    if macho_slots is not None:
        # Install names now; re-signing is batched by StreamRelocator.finish
        if not _relocate_macho(path, macho_slots, subs):
            return None, None, False  # No substitution applied to the marked name

        return path, None, False

    if elf_args is not None:
        if not elf_args:
            return None, None, False  # No substitution applied to the marked string

        _run_patchelf(path, elf_args)
        return None, None, True

    if new_text is None:
        return None, None, False

    with _writable(path):
        path.write_bytes(new_text)

    return None, text_rel, False


def _relocate_files(
    paths: list[str],
    subs: dict[bytes, bytes],
    keg_root: str,
    allowed_text: frozenset[str] | None,
    skip_linkage: bool,
) -> tuple[list[Path], list[str], int]:
    """Fan `_process_file` out across a bounded thread pool, one task per file.

    The pool is capped at `_RELOCATE_WORKERS` (at most 8), this function owns
    only the concurrency and the bookkeeping; the first `RelocationError`
    cancels whatever is still queued.

    Args:
        paths: The files to relocate, as strings.
        subs: A mapping of placeholder bytes to their replacements.
        keg_root: The keg directory as a string, for computing relative paths.
        allowed_text: The manifest's changed_files set, or None to substitute
            any marker-bearing text file.
        skip_linkage: Whether to leave binary dynamic linkage untouched.

    Returns:
        (to_sign, discovered, elf_relocated): the rewritten Mach-O paths still
        needing a re-sign, the relative paths of substituted text files, and the
        number of ELFs whose linkage was rewritten.

    Raises:
        RelocationError: If any file could not be relocated.
    """
    to_sign: list[Path] = []
    discovered: list[str] = []
    elf_n = 0

    if not paths:
        return to_sign, discovered, elf_n

    executor = ThreadPoolExecutor(max_workers=_RELOCATE_WORKERS)
    futures = [
        executor.submit(_process_file, p, subs, keg_root, allowed_text, skip_linkage)
        for p in paths
    ]

    try:
        for fut in as_completed(futures):
            macho_path, text_rel, elf_done = (
                fut.result()
            )  # First RelocationError re-raises here
            if macho_path is not None:
                to_sign.append(macho_path)

            elif elf_done:
                elf_n += 1

            elif text_rel is not None:
                discovered.append(text_rel)

    except BaseException:
        # Cancel queued tasks
        executor.shutdown(cancel_futures=True)
        raise

    else:
        executor.shutdown()

    return to_sign, discovered, elf_n
