"""Binary builders and drivers shared by the relocator unit tests."""

from __future__ import annotations

import os
import struct
from pathlib import Path

from brewery.providers.receipt import RuntimeDependency
from brewery.providers.relocator import elf as elf_mod
from brewery.providers.relocator import files as files_mod
from brewery.providers.relocator import keg as keg_mod
from brewery.providers.relocator import macho as macho_mod
from brewery.providers.relocator import reader as reader_mod
from brewery.providers.relocator import substitutions as subs_mod
from brewery.providers.relocator import tools as tools_mod

_CPU_ARM64 = 0x0100000C
_MH_DYLIB = 0x6  # Filetype: value is irrelevant to parsing, but realistic


def _lc_dylib(cmd: int, name: str, bo: str = "<") -> bytes:
    """Create a dylib_command structure.

    Args:
        cmd: The command type.
        name: The name of the dynamic library.
        bo: The byte order (default: little-endian).

    Returns:
        The serialised dylib_command structure as bytes.
    """
    name_b = name.encode() + b"\x00"
    name_b += b"\x00" * ((-(24 + len(name_b))) % 8)  # 8-byte align
    cmdsize = 24 + len(name_b)

    # dylib_command: cmd, cmdsize, name.offset=24, timestamp, cur_ver, compat_ver
    return struct.pack(f"{bo}IIIIII", cmd, cmdsize, 24, 0, 0x10000, 0x10000) + name_b


def _lc_rpath(path: str, bo: str = "<") -> bytes:
    """Create an rpath_command structure.

    Args:
        path: The path to the rpath.
        bo: The byte order (default: little-endian).

    Returns:
        The serialised rpath_command structure as bytes.
    """
    p = path.encode() + b"\x00"
    p += b"\x00" * ((-(12 + len(p))) % 8)
    cmdsize = 12 + len(p)

    # rpath_command: cmd, cmdsize, path.offset=12
    return struct.pack(f"{bo}III", macho_mod._LC_RPATH, cmdsize, 12) + p


def _thin_macho(load_cmds: list[bytes], *, big_endian: bool = False) -> bytes:
    """Create a thin Mach-O binary.

    Args:
        load_cmds: The load commands to include in the binary.
        big_endian: Whether to use big-endian byte order (default: little-endian).

    Returns:
        The serialised thin Mach-O binary as bytes.
    """
    body = b"".join(load_cmds)
    bo = ">" if big_endian else "<"

    # mach_header_64: magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved
    header = struct.pack(
        f"{bo}IiiIIIII",
        macho_mod._MH_MAGIC_64,
        _CPU_ARM64,
        0,
        _MH_DYLIB,
        len(load_cmds),
        len(body),
        0,
        0,
    )

    return header + body


def _fat_macho(slices: list[bytes]) -> bytes:
    """Create a fat Mach-O binary.

    Args:
        slices: The slices to include in the binary.

    Returns:
        The serialised fat Mach-O binary as bytes.
    """
    # fat_header (BE): magic, nfat_arch; then fat_arch[] (BE), then slices.
    nfat = len(slices)
    header = struct.pack(">II", macho_mod._FAT_MAGIC, nfat)
    arches = b""

    # First slice starts after header + arch table.
    offset = 8 + nfat * 20
    payload = b""
    for i, sl in enumerate(slices):
        aligned = offset + ((-offset) % 16)
        payload += b"\x00" * (aligned - offset) + sl

        # fat_arch: cputype, cpusubtype, offset, size, align
        arches += struct.pack(">iiIII", _CPU_ARM64 + i, 0, aligned, len(sl), 4)
        offset = aligned + len(sl)

    return header + arches + payload


def _slots(path: Path) -> list:
    """Parse a Mach-O's install-name slots, offsets included.

    Args:
        path: The path to the Mach-O file.

    Returns:
        The file's _NameSlot list, one entry per load command per slice.
    """
    with path.open("rb") as fh:
        return macho_mod._collect_names(
            reader_mod._Reader(fh.fileno(), os.fstat(fh.fileno()).st_size)
        )


def _relocate_tree(
    keg: Path,
    *,
    prefix: Path,
    cellar: Path,
    repository: Path,
    skip_relocation: bool = False,
    extra_tokens: dict[str, str] | None = None,
    text_files: list[str] | None = None,
) -> keg_mod.RelocationResult:
    """Relocate the regular files of a keg built on disk by hand.

    The pour path streams, so `StreamRelocator` never sees a finished tree; the
    binary post-pass it defers to is `_relocate_files` + `_codesign`, which is
    what this drives. Symlinks are excluded; the stream substitutes their
    targets at creation, covered in `tests/integration/test_relocation.py`.

    Args:
        keg: The keg directory to walk.
        prefix: The new prefix to use.
        cellar: The new cellar path.
        repository: The new repository path.
        skip_relocation: Whether to leave binary dynamic linkage untouched.
        extra_tokens: Any extra tokens to use for substitution.
        text_files: The manifest's changed_files list, or None to scan.

    Returns:
        The keg's RelocationResult, with `symlinks_relocated` always 0.
    """
    subs = subs_mod.build_substitutions(prefix, cellar, repository, extra=extra_tokens)
    allowed = None if text_files is None else frozenset(text_files)
    regular = [
        str(p) for p in sorted(keg.rglob("*")) if not p.is_symlink() and p.is_file()
    ]

    to_sign, discovered, elf_n = files_mod._relocate_files(
        regular, subs, str(keg), allowed, skip_relocation
    )
    tools_mod._codesign(to_sign)
    changed = sorted(text_files) if text_files is not None else sorted(discovered)

    return keg_mod.RelocationResult(changed, len(to_sign), 0, elf_n)


def _dep(full_name: str, *, declared_directly: bool = True) -> RuntimeDependency:
    """Build a runtime dependency entry.

    Args:
        full_name: The dependency's full name.
        declared_directly: Whether the formula declares the dep itself.

    Returns:
        The RuntimeDependency entry.
    """
    return RuntimeDependency(
        full_name=full_name, version="1", declared_directly=declared_directly
    )


def _keg_with_dylib(root: Path, load_commands: list[bytes], name: str = "libfoo.dylib"):
    """Write a single Mach-O into a keg's lib/ dir and return (keg, dylib path).

    Args:
        root: The temp dir to build the keg under.
        load_commands: The load commands for the fake thin Mach-O.
        name: The dylib filename.

    Returns:
        A (keg_dir, dylib_path) tuple.
    """
    keg = root / "keg"
    (keg / "lib").mkdir(parents=True)
    dylib = keg / "lib" / name
    dylib.write_bytes(_thin_macho(load_commands))

    return keg, dylib


def _elf64(
    *,
    interp: str | None = None,
    rpath: str | None = None,
    runpath: str | None = None,
    extra: str | None = None,
    big_endian: bool = False,
) -> bytes:
    """Build a minimal but well-formed ELF64 for the relocation parser.

    A single identity-mapped PT_LOAD (vaddr == file offset) keeps DT_STRTAB's
    virtual address equal to its file offset, so the parser's vaddr->offset step
    is exercised without a realistic address layout.

    Args:
        interp: PT_INTERP interpreter path, or None to omit the segment.
        rpath: DT_RPATH string, or None to omit the entry.
        runpath: DT_RUNPATH string, or None to omit the entry.
        extra: An unreferenced string placed in the string table (to simulate a
            marker that is present in the file but not in any linkage string).
        big_endian: Emit a big-endian ELF (ELFDATA2MSB).

    Returns:
        The serialised ELF64 image as bytes.
    """
    bo = ">" if big_endian else "<"
    ei_data = 2 if big_endian else 1
    e_ident = b"\x7fELF" + bytes([2, ei_data, 1]) + b"\x00" * 9  # ELFCLASS64

    ehdr_size, phentsize = 64, 56
    phnum = 2 + (1 if interp is not None else 0)  # PT_LOAD + PT_DYNAMIC (+INTERP)
    phoff = ehdr_size
    cur = phoff + phnum * phentsize

    blob = b""

    if interp is not None:
        interp_off = cur
        interp_b = interp.encode() + b"\x00"
        blob += interp_b
        cur += len(interp_b)

    else:
        interp_off = 0

    # String table: leading NUL, then each referenced (and extra) string
    strtab_off = cur
    strtab = b"\x00"
    rpath_o = runpath_o = None
    if rpath is not None:
        rpath_o = len(strtab)
        strtab += rpath.encode() + b"\x00"

    if runpath is not None:
        runpath_o = len(strtab)
        strtab += runpath.encode() + b"\x00"

    if extra is not None:
        strtab += extra.encode() + b"\x00"

    blob += strtab
    cur += len(strtab)

    # Dynamic array (identity vaddr mapping: DT_STRTAB value == file offset)
    dyn_off = cur
    entries = [(elf_mod._DT_STRTAB, strtab_off)]
    if rpath_o is not None:
        entries.append((elf_mod._DT_RPATH, rpath_o))

    if runpath_o is not None:
        entries.append((elf_mod._DT_RUNPATH, runpath_o))

    entries.append((elf_mod._DT_NULL, 0))
    dyn = b"".join(struct.pack(f"{bo}qQ", tag, val) for tag, val in entries)
    blob += dyn
    dyn_len = len(dyn)
    total = dyn_off + dyn_len

    phdrs = struct.pack(
        f"{bo}IIQQQQQQ", elf_mod._PT_LOAD, 5, 0, 0, 0, total, total, 0x1000
    )
    if interp is not None:
        interp_sz = len(interp) + 1
        phdrs += struct.pack(
            f"{bo}IIQQQQQQ",
            elf_mod._PT_INTERP,
            4,
            interp_off,
            interp_off,
            interp_off,
            interp_sz,
            interp_sz,
            1,
        )

    phdrs += struct.pack(
        f"{bo}IIQQQQQQ",
        elf_mod._PT_DYNAMIC,
        6,
        dyn_off,
        dyn_off,
        dyn_off,
        dyn_len,
        dyn_len,
        8,
    )

    ehdr = e_ident + struct.pack(
        f"{bo}HHIQQQIHHHHHH",
        2,  # e_type ET_EXEC
        0x3E,  # e_machine x86-64
        1,  # e_version
        0,  # e_entry
        phoff,  # e_phoff
        0,  # e_shoff
        0,  # e_flags
        ehdr_size,  # e_ehsize
        phentsize,  # e_phentsize
        phnum,  # e_phnum
        0,  # e_shentsize
        0,  # e_shnum
        0,  # e_shstrndx
    )

    return ehdr + phdrs + blob


def _keg_with_elf(root: Path, name: str = "foo", **elf_kwargs) -> tuple[Path, Path]:
    """Write a single ELF into a keg's bin/ dir and return (keg, elf path).

    Args:
        root: The temp dir to build the keg under.
        name: The ELF filename.
        **elf_kwargs: Passed through to `_elf64`.

    Returns:
        A (keg_dir, elf_path) tuple.
    """
    keg = root / "keg"
    (keg / "bin").mkdir(parents=True)
    elf = keg / "bin" / name
    elf.write_bytes(_elf64(**elf_kwargs))

    return keg, elf
