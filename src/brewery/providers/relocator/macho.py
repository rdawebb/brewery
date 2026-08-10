"""Parse and rewrite the install names of a Mach-O binary."""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg relocation logic.

from __future__ import annotations

import contextlib
import os
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from brewery.core.errors import RelocationError

from .reader import _cstr, _Reader
from .substitutions import _PLACEHOLDER_MARKER_STR, _apply
from .tools import _run_tool, _writable

# Mach-O Constants
_MH_MAGIC = 0xFEEDFACE  # 32-bit, host byte order
_MH_MAGIC_64 = 0xFEEDFACF  # 64-bit, host byte order
_MH_CIGAM = 0xCEFAEDFE  # 32-bit, swapped
_MH_CIGAM_64 = 0xCFFAEDFE  # 64-bit, swapped
_FAT_MAGIC = 0xCAFEBABE  # Fat header, big-endian
_FAT_CIGAM = 0xBEBAFECA  # Fat header, big-endian, swapped
_FAT_MAGIC_64 = 0xCAFEBABF  # Fat header, big-endian
_FAT_CIGAM_64 = 0xBFBAFECA  # Fat header, big-endian, swapped

# Load command constants
_LC_REQ_DYLD = 0x80000000  # Load command flag indicating the command requires dyld
_LC_ID_DYLIB = 0x0D  # Load command indicating the dylib's identity
_LC_LOAD_DYLIB = 0x0C  # Load command indicating a dylib should be loaded
_LC_LOAD_WEAK_DYLIB = (
    0x18 | _LC_REQ_DYLD
)  # Load command indicating a weak dylib should be loaded
_LC_REEXPORT_DYLIB = (
    0x1F | _LC_REQ_DYLD
)  # Load command indicating a dylib should be re-exported
_LC_LAZY_LOAD_DYLIB = 0x20  # Load command indicating a dylib should be lazily loaded
_LC_LOAD_UPWARD_DYLIB = (
    0x23 | _LC_REQ_DYLD
)  # Load command indicating a dylib should be loaded upward
_LC_RPATH = 0x1C | _LC_REQ_DYLD  # Load command indicating the rpath should be set

# Load commands whose path strings reference dylibs
_DYLIB_LOAD_CMDS = frozenset(
    {
        _LC_LOAD_DYLIB,
        _LC_LOAD_WEAK_DYLIB,
        _LC_REEXPORT_DYLIB,
        _LC_LAZY_LOAD_DYLIB,
        _LC_LOAD_UPWARD_DYLIB,
    }
)

# All recognised Mach-O / fat magic numbers, in raw big-endian view
_MACHO_MAGICS = frozenset(
    {
        _MH_MAGIC,
        _MH_MAGIC_64,
        _MH_CIGAM,
        _MH_CIGAM_64,
        _FAT_MAGIC,
        _FAT_CIGAM,
        _FAT_MAGIC_64,
        _FAT_CIGAM_64,
    }
)

# A load-command block larger than this is a malformed header
_MAX_SIZEOFCMDS = 16 * 1024 * 1024  # 16 MiB

# Escape hatch: force every Mach-O through install_name_tool instead of the
# in-process rewriter
_NATIVE_MACHO = os.environ.get("BREWERY_NO_NATIVE_MACHO") != "1"


class NameKind(Enum):
    """Represents the kind of a Mach-O name."""

    ID = "id"  # LC_ID_DYLIB        -> install_name_tool -id NEW
    DYLIB = "dylib"  # LC_LOAD_*_DYLIB    -> install_name_tool -change OLD NEW
    RPATH = "rpath"  # LC_RPATH           -> install_name_tool -rpath OLD NEW


@dataclass(frozen=True)
class InstallName:
    """Represents an install name in a Mach-O file."""

    kind: NameKind
    value: str


@dataclass(frozen=True, slots=True)
class _NameSlot:
    """One install name plus the file region its string occupies."""

    kind: NameKind
    value: str
    offset: int  # File offset of the string's first byte
    limit: int  # Exclusive end of the writable region (cmd_off + cmdsize)


def is_macho(path: Path) -> bool:
    """True if the file begins with a Mach-O or fat magic number.

    Args:
        path: The path to the file to check.

    Returns:
        True if the file is a Mach-O or fat binary, False otherwise.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(4)

    except OSError:
        return False

    if len(head) < 4:
        return False

    magic = struct.unpack(">I", head)[0]  # Raw big-endian view of the 4 bytes

    return magic in _MACHO_MAGICS


def _parse_thin(reader: _Reader, base: int) -> list[_NameSlot]:
    """Parse one thin Mach-O slice starting at `base`.

    The load commands are read in one go and parsed in memory, but the offsets
    recorded on each slot stay absolute; they are what `_patch_macho` writes
    back to.

    Args:
        reader: The reader for the file.
        base: The base offset to start parsing.

    Returns:
        A list of _NameSlot objects found in the slice.

    Raises:
        ValueError: If the header claims an implausible load-command size.
    """
    header = reader.read(base, 32)

    # Read the magic in each byte order
    le_magic = struct.unpack_from("<I", header, 0)[0]
    be_magic = struct.unpack_from(">I", header, 0)[0]
    if le_magic in (_MH_MAGIC_64, _MH_MAGIC):
        bo, is64 = "<", le_magic == _MH_MAGIC_64

    elif be_magic in (_MH_MAGIC_64, _MH_MAGIC):
        bo, is64 = ">", be_magic == _MH_MAGIC_64

    else:
        return []

    header_size = 32 if is64 else 28

    # mach_header[_64]: magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds...
    ncmds, sizeofcmds = struct.unpack_from(f"{bo}II", header, 16)
    if sizeofcmds > _MAX_SIZEOFCMDS:
        raise ValueError(f"load commands claim {sizeofcmds} bytes")

    cmds_at = base + header_size
    cmds = reader.read(cmds_at, sizeofcmds)

    names: list[_NameSlot] = []
    off = 0
    for _ in range(ncmds):
        if off + 8 > len(cmds):
            break  # Truncated, or ncmds outruns sizeofcmds

        cmd, cmdsize = struct.unpack_from(f"{bo}II", cmds, off)
        if cmdsize == 0:
            break  # Malformed

        if cmd == _LC_ID_DYLIB or cmd in _DYLIB_LOAD_CMDS:
            # dylib_command: cmd, cmdsize, name.offset, timestamp, cur, compat
            name_off = struct.unpack_from(f"{bo}I", cmds, off + 8)[0]
            start, end = off + name_off, off + cmdsize
            s = _cstr(cmds, start, end)
            kind = NameKind.ID if cmd == _LC_ID_DYLIB else NameKind.DYLIB
            names.append(
                _NameSlot(
                    kind,
                    s.decode("utf-8", "surrogateescape"),
                    cmds_at + start,
                    cmds_at + end,
                )
            )

        elif cmd == _LC_RPATH:
            # rpath_command: cmd, cmdsize, path.offset
            path_off = struct.unpack_from(f"{bo}I", cmds, off + 8)[0]
            start, end = off + path_off, off + cmdsize
            s = _cstr(cmds, start, end)
            names.append(
                _NameSlot(
                    NameKind.RPATH,
                    s.decode("utf-8", "surrogateescape"),
                    cmds_at + start,
                    cmds_at + end,
                )
            )
        off += cmdsize

    return names


def _collect_names(reader: _Reader) -> list[_NameSlot]:
    """Parse every dylib/rpath install name from an open file.

    Dispatches between fat and thin layouts; shared by `find_install_names`
    (one-shot, opens its own file) and the keg walker (reuses the reader it
    already holds).

    Args:
        reader: A reader positioned over the whole file.

    Returns:
        A list of _NameSlot objects found in the file.
    """
    raw_magic = reader.unpack_from(">I", 0)[0]

    if raw_magic in (_FAT_MAGIC, _FAT_CIGAM, _FAT_MAGIC_64, _FAT_CIGAM_64):
        is64 = raw_magic in (_FAT_MAGIC_64, _FAT_CIGAM_64)
        nfat = reader.unpack_from(">I", 4)[0]  # Fat header is BE
        names: list[_NameSlot] = []
        arch_off = 8

        for _ in range(nfat):
            if is64:
                # fat_arch_64: cputype, cpusubtype, offset(8), size(8)...
                offset = reader.unpack_from(">Q", arch_off + 8)[0]
                arch_off += 32

            else:
                # fat_arch: cputype, cpusubtype, offset(4), size(4), align
                offset = reader.unpack_from(">I", arch_off + 8)[0]
                arch_off += 20
            names.extend(_parse_thin(reader, offset))

        return names

    return _parse_thin(reader, 0)


def find_install_names(path: Path) -> list[InstallName]:
    """Return every dylib/rpath install name in a Mach-O (handles fat binaries).

    Identical names shared across fat slices collapse to one entry.

    Args:
        path: The path to the Mach-O file.

    Returns:
        A list of InstallName objects found in the file.
    """
    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size == 0:
            return []

        return list(
            dict.fromkeys(
                InstallName(slot.kind, slot.value)
                for slot in _collect_names(_Reader(fh.fileno(), size))
            )
        )


def _changed_name(value: str, subs: dict[bytes, bytes]) -> tuple[bytes, bytes] | None:
    """Substitute one install name, or None if no placeholder in it matched.

    The native planner sizes the new bytes against the slot's padding, the
    install_name_tool fallback passes them as an argument.

    Args:
        value: The install name as parsed from the header.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        (old, new) as bytes, or None if the name is unchanged.
    """
    old_b = value.encode("utf-8", "surrogateescape")
    new_b = _apply(old_b, subs)

    return None if new_b == old_b else (old_b, new_b)


def _build_macho_args(names: list[_NameSlot], subs: dict[bytes, bytes]) -> list[str]:
    """Build the install_name_tool argument list for a Mach-O file.

    The fallback path, for files the in-process rewriter declines; names shared
    across fat slices yield one argument pair, since install_name_tool rewrites
    every matching command in every slice.

    Args:
        names: The install names parsed from the file.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        The install_name_tool arguments (empty if nothing needs rewriting).
    """
    args: list[str] = []
    for name in dict.fromkeys(InstallName(n.kind, n.value) for n in names):
        old = name.value
        changed = _changed_name(old, subs)
        if changed is None:
            continue

        new_s = changed[1].decode("utf-8", "surrogateescape")
        if name.kind is NameKind.ID:
            args += ["-id", new_s]

        elif name.kind is NameKind.RPATH:
            args += ["-rpath", old, new_s]

        else:
            args += ["-change", old, new_s]

    return args


def _run_install_name_tool(path: Path, args: list[str]) -> None:
    """Rewrite the install names of one Mach-O file.

    This invalidates the code signature; the caller must ad-hoc re-sign the
    file (see `_codesign`) before it is used.

    Args:
        path: The path to the Mach-O file.
        args: A non-empty install_name_tool argument list.

    Raises:
        RelocationError: If install_name_tool fails.
    """
    with _writable(path):
        # Most likely failure: header pad exhausted (load command too large)
        _run_tool(
            ["install_name_tool", *args, str(path)],
            path=path,
            tool="install_name_tool",
            hint="install the Xcode Command Line Tools",
        )


def _plan_macho_patches(
    slots: list[_NameSlot], subs: dict[bytes, bytes]
) -> list[tuple[int, bytes]] | None:
    """Plan the in-place byte writes that relocate a Mach-O's install names.

    A load command's path string is NUL-padded out to `cmdsize`, so a
    replacement that fits in `[offset, limit)` can be written over the top with
    no change to the file's layout.

    Args:
        slots: The install-name slots parsed from the file.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        The (offset, bytes) writes, empty if nothing needs rewriting, or None if
        any replacement is too long for its region and falls back to install_name_tool.
    """
    patches: list[tuple[int, bytes]] = []
    for slot in slots:
        changed = _changed_name(slot.value, subs)
        if changed is None:
            continue

        new_b = changed[1]
        room = slot.limit - slot.offset
        if len(new_b) + 1 > room:  # +1 for the NUL terminator
            return None

        patches.append((slot.offset, new_b + b"\x00" * (room - len(new_b))))

    return patches


def _patch_macho(path: Path, patches: list[tuple[int, bytes]]) -> None:
    """Apply planned install-name writes to a Mach-O in place.

    Args:
        path: The path to the Mach-O file.
        patches: A non-empty list of (offset, bytes) writes.

    Raises:
        OSError: If the file could not be opened or written.
    """
    with _writable(path), path.open("r+b") as fh:
        for offset, data in patches:
            os.pwrite(fh.fileno(), data, offset)


def _verify_macho(path: Path) -> bool:
    """Re-read a patched Mach-O and confirm no install name kept a placeholder.

    Guard against a mis-parsed header having sent a write to the wrong offset.

    Args:
        path: The path to the patched Mach-O file.

    Returns:
        True if every install name is placeholder-free.
    """
    try:
        with path.open("rb") as fh:
            reader = _Reader(fh.fileno(), os.fstat(fh.fileno()).st_size)

            return all(
                _PLACEHOLDER_MARKER_STR not in slot.value
                for slot in _collect_names(reader)
            )

    except (OSError, ValueError, struct.error):
        return False


def _relocate_macho(
    path: Path, slots: list[_NameSlot], subs: dict[bytes, bytes]
) -> bool:
    """Rewrite one Mach-O's install names, in process where possible.

    Falls back to install_name_tool when a replacement outgrows the padding its
    load command reserves, or when the in-place write does not verify.

    Args:
        path: The path to the Mach-O file.
        slots: The install-name slots parsed from the file.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        True if the file was rewritten, False if it needed no change.

    Raises:
        RelocationError: If the fallback rewrite fails.
    """
    if _NATIVE_MACHO:
        patches = _plan_macho_patches(slots, subs)
        if patches is not None:
            if not patches:
                return False  # Marker present, but not in any install name

            try:
                with path.open("rb") as fh:
                    original = [
                        (off, os.pread(fh.fileno(), len(data), off))
                        for off, data in patches
                    ]
                _patch_macho(path, patches)

            except OSError as exc:
                raise RelocationError(path, f"install-name rewrite failed: {exc}")

            if _verify_macho(path):
                return True

            # Put the file back the way it was and let the real tool try
            with contextlib.suppress(OSError):
                _patch_macho(path, original)

    args = _build_macho_args(slots, subs)
    if not args:
        return False

    _run_install_name_tool(path, args)

    return True
