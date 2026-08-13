"""Parse and rewrite the dynamic linkage of an ELF binary, via patchelf."""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg relocation logic.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .reader import _Reader
from .substitutions import _substitute
from .tools import _run_tool, _writable

# ELF constants (Linux dynamic linkage; rewritten via patchelf, no signing)
_ELF_MAGIC = b"\x7fELF"
_ELFCLASS32, _ELFCLASS64 = 1, 2  # ELF class (32-bit or 64-bit)
_ELFDATA2LSB = 1  # little-endian; anything else is treated big-endian
_PT_LOAD = 1  # Load segment
_PT_DYNAMIC = 2  # Dynamic linking segment
_PT_INTERP = 3  # Interpreter segment
_DT_NULL = 0  # Null entry
_DT_STRTAB = 5  # String table
_DT_RPATH = 15  # RPATH entry
_DT_RUNPATH = 29  # RUNPATH entry


@dataclass(frozen=True)
class _ElfInfo:
    """The dynamic-linkage strings an ELF relocation may need to rewrite."""

    interp: str | None  # PT_INTERP (the ELF interpreter / dynamic loader)
    rpath: str | None  # DT_RPATH
    runpath: str | None  # DT_RUNPATH


def _vaddr_to_offset(vaddr: int, loads: list[tuple[int, int, int]]) -> int | None:
    """Translate a virtual address to a file offset via the PT_LOAD segments.

    Args:
        vaddr: The virtual address to translate (e.g. DT_STRTAB).
        loads: The (vaddr, offset, filesz) tuples of the PT_LOAD segments.

    Returns:
        The file offset, or None if no segment maps the address.
    """
    for seg_vaddr, seg_off, seg_filesz in loads:
        if seg_vaddr <= vaddr < seg_vaddr + seg_filesz:
            return seg_off + (vaddr - seg_vaddr)

    return None


def _read_elf(reader: _Reader) -> _ElfInfo:
    """Parse an ELF's interpreter and RPATH/RUNPATH strings.

    Resolves the string-table offsets it finds to file offsets via the PT_LOAD
    map; unparseable or missing fields yield None entries rather than raising.

    Args:
        reader: A reader positioned over the whole file.

    Returns:
        The parsed _ElfInfo (fields may be None).
    """
    is64 = reader.byte(4) == _ELFCLASS64
    if not is64 and reader.byte(4) != _ELFCLASS32:
        return _ElfInfo(None, None, None)

    bo = "<" if reader.byte(5) == _ELFDATA2LSB else ">"

    if is64:
        e_phoff = reader.unpack_from(f"{bo}Q", 32)[0]
        e_phentsize, e_phnum = reader.unpack_from(f"{bo}HH", 54)

    else:
        e_phoff = reader.unpack_from(f"{bo}I", 28)[0]
        e_phentsize, e_phnum = reader.unpack_from(f"{bo}HH", 42)

    if e_phoff == 0 or e_phnum == 0:
        return _ElfInfo(None, None, None)

    interp: str | None = None
    dyn_off = dyn_size = 0
    loads: list[tuple[int, int, int]] = []

    for i in range(e_phnum):
        ph = e_phoff + i * e_phentsize
        p_type = reader.unpack_from(f"{bo}I", ph)[0]
        if is64:
            p_offset = reader.unpack_from(f"{bo}Q", ph + 8)[0]
            p_vaddr = reader.unpack_from(f"{bo}Q", ph + 16)[0]
            p_filesz = reader.unpack_from(f"{bo}Q", ph + 32)[0]

        else:
            p_offset = reader.unpack_from(f"{bo}I", ph + 4)[0]
            p_vaddr = reader.unpack_from(f"{bo}I", ph + 8)[0]
            p_filesz = reader.unpack_from(f"{bo}I", ph + 16)[0]

        if p_type == _PT_INTERP:
            interp = reader.cstr(p_offset, p_offset + p_filesz).decode(
                "utf-8", "surrogateescape"
            )

        elif p_type == _PT_DYNAMIC:
            dyn_off, dyn_size = p_offset, p_filesz

        elif p_type == _PT_LOAD:
            loads.append((p_vaddr, p_offset, p_filesz))

    if dyn_off == 0:
        return _ElfInfo(interp, None, None)

    # Dynamic array: Elf(32|64)_Dyn is {d_tag, d_un}, both word-sized
    ent_size = 16 if is64 else 8
    word = "Q" if is64 else "I"
    strtab_vaddr: int | None = None
    rpath_off: int | None = None
    runpath_off: int | None = None

    for i in range(dyn_size // ent_size):
        off = dyn_off + i * ent_size
        d_tag, d_val = reader.unpack_from(f"{bo}{word}{word}", off)
        if d_tag == _DT_NULL:
            break

        if d_tag == _DT_STRTAB:
            strtab_vaddr = d_val

        elif d_tag == _DT_RPATH:
            rpath_off = d_val

        elif d_tag == _DT_RUNPATH:
            runpath_off = d_val

    if strtab_vaddr is None:
        return _ElfInfo(interp, None, None)

    strtab = _vaddr_to_offset(strtab_vaddr, loads)
    if strtab is None:
        return _ElfInfo(interp, None, None)

    def _s(o: int | None) -> str | None:
        if o is None:
            return None

        return reader.cstr(strtab + o, reader.size).decode("utf-8", "surrogateescape")

    return _ElfInfo(interp, _s(rpath_off), _s(runpath_off))


def _rewrite_str(path: Path, old: str, subs: dict[bytes, bytes]) -> str | None:
    """Substitute placeholders in an ELF linkage string; None if unchanged.

    Args:
        path: The ELF file (for the error message).
        old: The current string (an rpath or interpreter path).
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        The rewritten string, or None if no substitution applied.

    Raises:
        RelocationError: If a placeholder survived substitution.
    """
    new_b = _substitute(path, old.encode("utf-8", "surrogateescape"), subs)

    return None if new_b is None else new_b.decode("utf-8", "surrogateescape")


def _build_elf_args(path: Path, info: _ElfInfo, subs: dict[bytes, bytes]) -> list[str]:
    """Build the patchelf argument list for an ELF file.

    Rewrites the interpreter and the run-time search path; DT_RUNPATH takes
    precedence over DT_RPATH at load time, so it is preferred when present.

    Args:
        path: The ELF file (for error messages).
        info: The parsed interpreter / rpath / runpath strings.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        The patchelf arguments (empty if nothing needs rewriting).
    """
    args: list[str] = []

    if info.interp:
        new_interp = _rewrite_str(path, info.interp, subs)
        if new_interp is not None:
            args += ["--set-interpreter", new_interp]

    search_path = info.runpath if info.runpath is not None else info.rpath
    if search_path:
        new_path = _rewrite_str(path, search_path, subs)
        if new_path is not None:
            args += ["--set-rpath", new_path]

    return args


def _run_patchelf(path: Path, args: list[str]) -> None:
    """Rewrite the dynamic linkage of one ELF file. No re-signing on Linux.

    Args:
        path: The path to the ELF file.
        args: A non-empty patchelf argument list.

    Raises:
        RelocationError: If patchelf fails.
    """
    with _writable(path):
        _run_tool(
            ["patchelf", *args, str(path)],
            path=path,
            tool="patchelf",
            hint="install it to relocate ELF binaries",
        )
