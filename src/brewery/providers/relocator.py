"""Relocate a staged Homebrew bottle keg into a target prefix."""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg relocation logic.

from __future__ import annotations

import contextlib
import mmap
import os
import re
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from brewery.core.errors import RelocationError
from brewery.core.host import current_platform, preferred_perl_version
from brewery.providers.receipt import RuntimeDependency

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

_PLACEHOLDER_MARKER = b"@@HOMEBREW_"
_AR_MAGIC = b"!<arch>\n"  # Static archive (ar) magic

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

# Matches brew's Version.formula_optionally_versioned_regex(:openjdk)
_OPENJDK_RE = re.compile(r"\Aopenjdk(@\d+(?:\.\d+)*)?\Z")

# Guards the tab's `preferred_perl` before it is pasted into a shebang path
_PERL_VERSION_RE = re.compile(r"\A\d+\.\d+\Z")

# JAVA_HOME within an openjdk keg: macOS nests it in the .jdk bundle, while
# Linux keeps it directly under libexec (brew's per-OS Keg override)
_MACOS_JAVA_HOME_SUFFIX = "libexec/openjdk.jdk/Contents/Home"
_LINUX_JAVA_HOME_SUFFIX = "libexec"

# Bounded thread pool for the regular-file relocation phase
_RELOCATE_WORKERS = min(8, os.cpu_count() or 4)

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


class NameKind(Enum):
    """Represents the kind of a Mach-O name."""

    ID = "id"  # LC_ID_DYLIB        -> install_name_tool -id NEW
    DYLIB = "dylib"  # LC_LOAD_*_DYLIB    -> install_name_tool -change OLD NEW
    RPATH = "rpath"  # LC_RPATH           -> install_name_tool -rpath OLD NEW


class _Kind(Enum):
    """Internal file classification for the fused relocation path."""

    MACHO = "macho"
    ELF = "elf"
    ARCHIVE = "archive"
    TEXT = "text"


@dataclass(frozen=True)
class InstallName:
    """Represents an install name in a Mach-O file."""

    kind: NameKind
    value: str


def build_substitutions(
    prefix: Path,
    cellar: Path,
    repository: Path,
    *,
    extra: dict[str, str] | None = None,
) -> dict[bytes, bytes]:
    """Return the placeholder->value map as bytes (longest token first).

    `extra` carries formula-specific tokens such as `@@HOMEBREW_PERL@@` /
    `@@HOMEBREW_JAVA@@` whose values the pipeline must resolve per formula;
    omit them and any placeholder is left untouched.

    Args:
        prefix: The Homebrew prefix path.
        cellar: The Homebrew cellar path.
        repository: The Homebrew repository path.
        extra: Additional formula-specific tokens to include.

    Returns:
        A mapping of placeholder bytes to their resolved values.
    """
    subs: dict[bytes, bytes] = {
        b"@@HOMEBREW_PREFIX@@": str(prefix).encode(),
        b"@@HOMEBREW_CELLAR@@": str(cellar).encode(),
        b"@@HOMEBREW_REPOSITORY@@": str(repository).encode(),
        b"@@HOMEBREW_LIBRARY@@": str(repository / "Library").encode(),
    }
    if extra:
        subs.update({k.encode(): v.encode() for k, v in extra.items()})

    # Substitute longer tokens first so no token is a prefix-collision risk
    return dict(sorted(subs.items(), key=lambda kv: len(kv[0]), reverse=True))


def _perl_path(
    prefix: Path, brewed: bool, built_on: dict[str, object] | None, *, is_linux: bool
) -> str:
    """Resolve the `@@HOMEBREW_PERL@@` override path.

    Args:
        prefix: The Homebrew prefix path.
        brewed: Whether the formula depends on the brewed perl.
        built_on: The bottle tab's `built_on` block, if any.
        is_linux: Whether the host is Linux (unversioned system perl).

    Returns:
        The absolute path to the perl interpreter.
    """
    if brewed:
        return str(prefix / "opt" / "perl" / "bin" / "perl")

    if is_linux:
        return "/usr/bin/perl"

    built_version = (built_on or {}).get("preferred_perl")
    if isinstance(built_version, str) and _PERL_VERSION_RE.match(built_version):
        candidate = f"/usr/bin/perl{built_version}"
        if Path(candidate).exists():
            return candidate

    return f"/usr/bin/perl{preferred_perl_version()}"


def formula_tokens(
    prefix: Path,
    *,
    name: str,
    runtime_deps: list[RuntimeDependency],
    built_on: dict[str, object] | None = None,
) -> dict[str, str]:
    """Resolve the formula-specific placeholders for one keg.

    Args:
        prefix: The Homebrew prefix path.
        name: The formula name.
        runtime_deps: The formula's runtime dependency entries.
        built_on: The bottle tab's `built_on` block, if any.

    Returns:
        The formula-specific token map, suitable for `relocate_keg`'s `extra_tokens`.
    """
    plat = current_platform()
    is_linux = plat is not None and plat.os == "linux"

    brewed_perl = name == "perl" or any(
        d.full_name == "perl" and d.declared_directly for d in runtime_deps
    )
    tokens = {
        "@@HOMEBREW_PERL@@": _perl_path(
            prefix, brewed_perl, built_on, is_linux=is_linux
        )
    }

    openjdk = next(
        (d.full_name for d in runtime_deps if _OPENJDK_RE.match(d.full_name)), None
    )
    if openjdk:
        suffix = _LINUX_JAVA_HOME_SUFFIX if is_linux else _MACOS_JAVA_HOME_SUFFIX
        tokens["@@HOMEBREW_JAVA@@"] = str(prefix / "opt" / openjdk / suffix)

    return tokens


def _reject_unresolved(path: Path, value: bytes) -> None:
    """Raise if a placeholder survived substitution.

    Failing here aborts the native install and lets the caller fall back
    to brew, rather than shipping a broken keg into the Cellar.

    Args:
        path: The file being relocated, for the error message.
        value: The substituted bytes.

    Raises:
        RelocationError: If a placeholder remains.
    """
    start = value.find(_PLACEHOLDER_MARKER)
    if start == -1:
        return

    end = value.find(b"@@", start + len(_PLACEHOLDER_MARKER))
    token = value[start : end + 2] if end != -1 else value[start : start + 40]
    raise RelocationError(
        path, f"unresolved placeholder {token.decode('utf-8', 'replace')}"
    )


def _apply(value: bytes, subs: dict[bytes, bytes]) -> bytes:
    """Apply substitutions to a byte string.

    Args:
        value: The byte string to modify.
        subs: The substitution mapping.

    Returns:
        The modified byte string.
    """
    for token, repl in subs.items():
        if token in value:
            value = value.replace(token, repl)

    return value


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


def _read_cstr(data: mmap.mmap, start: int, end: int) -> bytes:
    """Read a null-terminated string from a mapping.

    Args:
        data: The mapping to read from.
        start: The start index (inclusive).
        end: The end index (exclusive).

    Returns:
        The null-terminated string as bytes.
    """
    nul = bytes(data[start:end]).find(b"\x00")

    return bytes(data[start : start + nul]) if nul != -1 else bytes(data[start:end])


def _parse_thin(data: mmap.mmap, base: int) -> list[InstallName]:
    """Parse one thin Mach-O slice starting at `base`.

    Args:
        data: The mapping to read from.
        base: The base offset to start parsing.

    Returns:
        A list of InstallName objects found in the slice.
    """
    # Read the magic in each byte order
    le_magic = struct.unpack_from("<I", data, base)[0]
    be_magic = struct.unpack_from(">I", data, base)[0]
    if le_magic in (_MH_MAGIC_64, _MH_MAGIC):
        bo, is64 = "<", le_magic == _MH_MAGIC_64

    elif be_magic in (_MH_MAGIC_64, _MH_MAGIC):
        bo, is64 = ">", be_magic == _MH_MAGIC_64

    else:
        return []

    header_size = 32 if is64 else 28

    # mach_header[_64]: magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds...
    ncmds = struct.unpack_from(f"{bo}I", data, base + 16)[0]

    names: list[InstallName] = []
    cmd_off = base + header_size
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from(f"{bo}II", data, cmd_off)
        if cmdsize == 0:
            break  # Malformed

        if cmd == _LC_ID_DYLIB or cmd in _DYLIB_LOAD_CMDS:
            # dylib_command: cmd, cmdsize, name.offset, timestamp, cur, compat
            name_off = struct.unpack_from(f"{bo}I", data, cmd_off + 8)[0]
            s = _read_cstr(data, cmd_off + name_off, cmd_off + cmdsize)
            kind = NameKind.ID if cmd == _LC_ID_DYLIB else NameKind.DYLIB
            names.append(InstallName(kind, s.decode("utf-8", "surrogateescape")))

        elif cmd == _LC_RPATH:
            # rpath_command: cmd, cmdsize, path.offset
            path_off = struct.unpack_from(f"{bo}I", data, cmd_off + 8)[0]
            s = _read_cstr(data, cmd_off + path_off, cmd_off + cmdsize)
            names.append(
                InstallName(NameKind.RPATH, s.decode("utf-8", "surrogateescape"))
            )
        cmd_off += cmdsize

    return names


def _collect_names(data: mmap.mmap) -> list[InstallName]:
    """Parse every dylib/rpath install name from a live mapping.

    Dispatches between fat and thin layouts. Shared by `find_install_names`
    (one-shot, opens its own mapping) and the keg walker (reuses the
    mapping it already holds open).

    Args:
        data: A readable mapping positioned at the start of the file.

    Returns:
        A list of InstallName objects found in the file.
    """
    raw_magic = struct.unpack_from(">I", data, 0)[0]

    if raw_magic in (_FAT_MAGIC, _FAT_CIGAM, _FAT_MAGIC_64, _FAT_CIGAM_64):
        is64 = raw_magic in (_FAT_MAGIC_64, _FAT_CIGAM_64)
        nfat = struct.unpack_from(">I", data, 4)[0]  # Fat header is BE
        names: list[InstallName] = []
        arch_off = 8

        for _ in range(nfat):
            if is64:
                # fat_arch_64: cputype, cpusubtype, offset(8), size(8)...
                offset = struct.unpack_from(">Q", data, arch_off + 8)[0]
                arch_off += 32

            else:
                # fat_arch: cputype, cpusubtype, offset(4), size(4), align
                offset = struct.unpack_from(">I", data, arch_off + 8)[0]
                arch_off += 20
            names.extend(_parse_thin(data, offset))

        # De-duplicate identical names shared across slices
        return list(dict.fromkeys(names))

    return _parse_thin(data, 0)


def find_install_names(path: Path) -> list[InstallName]:
    """Return every dylib/rpath install name in a Mach-O (handles fat binaries).

    Args:
        path: The path to the Mach-O file.

    Returns:
        A list of InstallName objects found in the file.
    """
    with path.open("rb") as fh:
        if os.fstat(fh.fileno()).st_size == 0:
            return []

        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            return _collect_names(mm)


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


def _read_elf(mm: mmap.mmap) -> _ElfInfo:
    """Parse an ELF's interpreter and RPATH/RUNPATH strings.

    Mirrors the in-process Mach-O parser: reads the program headers for
    PT_INTERP / PT_DYNAMIC, walks the dynamic array for DT_RPATH / DT_RUNPATH,
    and resolves those string-table offsets to file offsets via the PT_LOAD
    map. Unparseable or missing fields yield None entries rather than raising.

    Args:
        mm: A readable mapping positioned at the start of the file.

    Returns:
        The parsed _ElfInfo (fields may be None).
    """
    is64 = mm[4] == _ELFCLASS64
    if not is64 and mm[4] != _ELFCLASS32:
        return _ElfInfo(None, None, None)

    bo = "<" if mm[5] == _ELFDATA2LSB else ">"

    if is64:
        e_phoff = struct.unpack_from(f"{bo}Q", mm, 32)[0]
        e_phentsize, e_phnum = struct.unpack_from(f"{bo}HH", mm, 54)

    else:
        e_phoff = struct.unpack_from(f"{bo}I", mm, 28)[0]
        e_phentsize, e_phnum = struct.unpack_from(f"{bo}HH", mm, 42)

    if e_phoff == 0 or e_phnum == 0:
        return _ElfInfo(None, None, None)

    interp: str | None = None
    dyn_off = dyn_size = 0
    loads: list[tuple[int, int, int]] = []

    for i in range(e_phnum):
        ph = e_phoff + i * e_phentsize
        p_type = struct.unpack_from(f"{bo}I", mm, ph)[0]
        if is64:
            p_offset = struct.unpack_from(f"{bo}Q", mm, ph + 8)[0]
            p_vaddr = struct.unpack_from(f"{bo}Q", mm, ph + 16)[0]
            p_filesz = struct.unpack_from(f"{bo}Q", mm, ph + 32)[0]

        else:
            p_offset = struct.unpack_from(f"{bo}I", mm, ph + 4)[0]
            p_vaddr = struct.unpack_from(f"{bo}I", mm, ph + 8)[0]
            p_filesz = struct.unpack_from(f"{bo}I", mm, ph + 16)[0]

        if p_type == _PT_INTERP:
            interp = _read_cstr(mm, p_offset, p_offset + p_filesz).decode(
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
        d_tag, d_val = struct.unpack_from(f"{bo}{word}{word}", mm, off)
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

        return _read_cstr(mm, strtab + o, mm.size()).decode("utf-8", "surrogateescape")

    return _ElfInfo(interp, _s(rpath_off), _s(runpath_off))


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


@contextlib.contextmanager
def _writable(path: Path):
    """Temporarily add the owner-write bit, restoring the original mode after.

    Args:
        path: The path to the file to modify.
    """
    mode = path.stat().st_mode
    needs = not mode & 0o200
    if needs:
        os.chmod(path, mode | 0o200)

    try:
        yield

    finally:
        if needs:
            # The file may have been recreated, so only restore if it still exists
            with contextlib.suppress(FileNotFoundError):
                os.chmod(path, mode)


def _build_macho_args(names: list[InstallName], subs: dict[bytes, bytes]) -> list[str]:
    """Build the install_name_tool argument list for a Mach-O file.

    Args:
        names: The install names parsed from the file.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        The install_name_tool arguments (empty if nothing needs rewriting).
    """
    args: list[str] = []
    for name in names:
        old = name.value
        old_b = old.encode("utf-8", "surrogateescape")
        new_b = _apply(old_b, subs)
        if new_b == old_b:
            continue  # No placeholder in this entry

        new_s = new_b.decode("utf-8", "surrogateescape")
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
        try:
            _run(["install_name_tool", *args, str(path)])
        except subprocess.CalledProcessError as exc:
            # Most likely cause: header pad exhausted (load command too large)
            raise RelocationError(
                path, f"install_name_tool failed: {exc.stderr.strip()}"
            )

        except FileNotFoundError:
            raise RelocationError(
                path,
                "install_name_tool not found on PATH; "
                "install the Xcode Command Line Tools",
            )


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
    old_b = old.encode("utf-8", "surrogateescape")
    new_b = _apply(old_b, subs)

    # Any surviving placeholder is a broken keg, whether or not a sub applied
    _reject_unresolved(path, new_b)
    if new_b == old_b:
        return None

    return new_b.decode("utf-8", "surrogateescape")


def _build_elf_args(path: Path, info: _ElfInfo, subs: dict[bytes, bytes]) -> list[str]:
    """Build the patchelf argument list for an ELF file.

    Rewrites the interpreter and the run-time search path. DT_RUNPATH takes
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
        try:
            _run(["patchelf", *args, str(path)])

        except subprocess.CalledProcessError as exc:
            raise RelocationError(path, f"patchelf failed: {exc.stderr.strip()}")

        except FileNotFoundError:
            raise RelocationError(
                path, "patchelf not found on PATH; install it to relocate ELF binaries"
            )


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


def _codesign(paths: list[Path]) -> None:
    """Ad-hoc re-sign a batch of Mach-O files.

    The batch is chunked to stay under ARG_MAX. Each file needs its owner-write bit for the
    in-place re-sign, so the whole chunk is made writable for the duration of the call.

    Args:
        paths: The Mach-O files to re-sign (may be empty).

    Raises:
        RelocationError: If codesign fails for any chunk.
    """
    for chunk in _chunk_paths(paths, _CODESIGN_ARG_BUDGET):
        with contextlib.ExitStack() as stack:
            for path in chunk:
                stack.enter_context(_writable(path))

            try:
                _run([*_CODESIGN_ARGS, *(str(p) for p in chunk)])
            except subprocess.CalledProcessError as exc:
                raise RelocationError(
                    chunk[0], f"codesign failed: {exc.stderr.strip()}"
                )

            except FileNotFoundError:
                raise RelocationError(
                    chunk[0],
                    "codesign not found on PATH; install the Xcode Command Line Tools",
                )


def relocate_text(path: Path, subs: dict[bytes, bytes]) -> bool:
    """Substitute placeholders in a text/script/config file. Returns True if
    the file was modified. Length changes are fine (file is rewritten).

    Args:
        path: The path to the text file.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        True if the file was modified, False otherwise.

    Raises:
        RelocationError: If a placeholder survived substitution.
    """
    data = path.read_bytes()
    if _PLACEHOLDER_MARKER not in data:
        return False

    new = _apply(data, subs)
    _reject_unresolved(path, new)
    if new == data:
        return False

    with _writable(path):
        path.write_bytes(new)

    return True


def relocate_symlink(link: Path, subs: dict[bytes, bytes]) -> bool:
    """Rewrite a symlink whose target contains a placeholder.

    Args:
        link: The path to the symlink.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        True if the symlink was modified, False otherwise.

    Raises:
        RelocationError: If a placeholder survived substitution.
    """
    target = os.readlink(link).encode("utf-8", "surrogateescape")
    if _PLACEHOLDER_MARKER not in target:
        return False

    new = _apply(target, subs)
    _reject_unresolved(link, new)
    if new == target:
        return False

    with _writable(link.parent):
        link.unlink()
        os.symlink(new.decode("utf-8", "surrogateescape"), link)

    return True


def _classify(mm: mmap.mmap) -> _Kind:
    """Classify a mapping as archive, Mach-O, or text.

    Called only after the marker gate has matched, so the mapping is at least
    `len(_PLACEHOLDER_MARKER)` (11) bytes & the magic reads are safe.

    Args:
        mm: A readable mapping positioned at the start of the file.

    Returns:
        The file's classification.
    """
    if mm[:8] == _AR_MAGIC:
        return _Kind.ARCHIVE

    if struct.unpack_from(">I", mm, 0)[0] in _MACHO_MAGICS:
        return _Kind.MACHO

    if mm[:4] == _ELF_MAGIC:
        return _Kind.ELF

    return _Kind.TEXT


def _process_file(
    path_str: str,
    subs: dict[bytes, bytes],
    keg_root: str,
    allowed_text: frozenset[str] | None,
    skip_linkage: bool = False,
) -> tuple[Path | None, str | None, bool]:
    """Relocate one regular (non-symlink) file via a single mmap.

    A rewritten Mach-O has its install names fixed but is left unsigned; the
    returned path is handed back to `relocate_keg`, which batches the ad-hoc
    re-sign across the whole keg. An ELF is rewritten in place via patchelf and
    needs no re-signing, so it is only counted.

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
    path = Path(path_str)
    macho_args: list[str] | None = None
    elf_args: list[str] | None = None
    new_text: bytes | None = None
    text_rel: str | None = None

    try:
        with path.open("rb") as fh:
            if os.fstat(fh.fileno()).st_size == 0:
                return None, None, False

            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                # No parse and no full-file read for marker-free files
                if mm.find(_PLACEHOLDER_MARKER) == -1:
                    return None, None, False

                kind = _classify(mm)
                if kind is _Kind.ARCHIVE:
                    # Length-changing text substitution would corrupt headers and offsets
                    raise RelocationError(path, "static archive contains a placeholder")

                if kind is _Kind.MACHO:
                    if skip_linkage:
                        return None, None, False  # :any_skip_relocation

                    macho_args = _build_macho_args(_collect_names(mm), subs)

                elif kind is _Kind.ELF:
                    if skip_linkage:
                        return None, None, False  # :any_skip_relocation

                    elf_args = _build_elf_args(path, _read_elf(mm), subs)

                else:
                    rel = path.relative_to(keg_root).as_posix()
                    # In manifest mode, only substitute files brew listed
                    if allowed_text is None or rel in allowed_text:
                        raw = bytes(mm)
                        new = _apply(raw, subs)
                        _reject_unresolved(path, new)
                        if new != raw:
                            new_text = new
                            text_rel = rel

    except OSError as exc:
        raise RelocationError(path, f"read failed: {exc}") from exc

    # Mapping is closed here, so safe to mutate the file
    if macho_args is not None:
        if not macho_args:
            return None, None, False  # Marker present but not in any install name

        # install_name_tool now; re-signing is batched by relocate_keg
        _run_install_name_tool(path, macho_args)
        return path, None, False

    if elf_args is not None:
        if not elf_args:
            return None, None, False  # Marker present but not in linkage strings

        _run_patchelf(path, elf_args)
        return None, None, True

    if new_text is None:
        return None, None, False

    with _writable(path):
        path.write_bytes(new_text)

    return None, text_rel, False


def _scan(root: Path) -> tuple[list[str], list[str]]:
    """Walks the keg with scandir, partitioning into symlinks and regular files.

    Uses cached DirEntry metadata to avoid a per-file lstat on APFS, and keeps
    paths as strings to defer Path construction.

    Args:
        root: The keg directory to walk.

    Returns:
        A tuple of (symlink paths, regular file paths).
    """
    symlinks: list[str] = []
    regular: list[str] = []
    stack = [str(root)]

    while stack:
        with os.scandir(stack.pop()) as it:
            for entry in it:
                if entry.is_symlink():  # Cached d_type, no lstat on APFS
                    symlinks.append(entry.path)

                elif entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)

                else:
                    regular.append(entry.path)  # Path deferred

    return symlinks, regular


@dataclass(frozen=True)
class RelocationResult:
    """Outcome of relocating a keg.

    `changed_files` is the sorted list of relative POSIX paths whose *text*
    content was substituted — the same set brew records as the receipt's
    `changed_files`. (Mach-O install-name rewrites are not included, matching
    brew.) When the manifest supplied the list, this echoes it; on the fallback
    scan it is what the relocator discovered, so the pipeline can feed it to the
    receipt when no tab was available.
    """

    changed_files: list[str]
    macho_relocated: int
    symlinks_relocated: int
    elf_relocated: int = 0


def relocate_keg(
    keg_dir: Path,
    *,
    prefix: Path,
    cellar: Path,
    repository: Path,
    skip_relocation: bool = False,
    extra_tokens: dict[str, str] | None = None,
    text_files: list[str] | None = None,
) -> RelocationResult:
    """Relocate an extracted keg in place.

    `skip_relocation` should be set from the catalog bottle's `cellar` value
    being `:any_skip_relocation`. When true the binary dynamic-linkage rewrite
    (Mach-O install names / ELF rpath) is skipped but text and symlink
    placeholder substitution still run.

    `text_files` is the manifest tab's `changed_files` (relative POSIX paths).
    When provided, only those files are text-substituted and the result's
    `changed_files` echoes the list (brew's authoritative set); pass None to
    fall back to substituting any marker-bearing text file and report what was
    discovered. Mach-O files and symlinks are processed regardless, since the
    tab does not enumerate them.

    Symlinks are processed serially, regular files run on a bounded thread pool,
    since each Mach-O forks subprocesses that release the GIL. The first
    RelocationError propagates and aborts, leaving the caller to fall back to brew.

    Args:
        keg_dir: The path to the keg directory.
        prefix: The new prefix to use.
        cellar: The new cellar path.
        repository: The new repository path.
        skip_relocation: Whether to skip relocation.
        extra_tokens: Any extra tokens to use for substitution.
        text_files: The manifest's changed_files list, or None to scan.

    Returns:
        A RelocationResult with the text changed_files, Mach-O count, and
        symlink count.

    Raises:
        RelocationError: If the relocation fails, or a listed text file is
            missing from the keg.
    """
    subs = build_substitutions(prefix, cellar, repository, extra=extra_tokens)
    keg_root = str(keg_dir)

    allowed_text: frozenset[str] | None = None
    if text_files is not None:
        allowed_text = frozenset(text_files)

        # Fail fast on a manifest/extract mismatch
        for rel in text_files:
            if not (keg_dir / rel).is_file():
                raise RelocationError(
                    keg_dir / rel, "manifest changed_files entry missing from keg"
                )

    symlinks, regular = _scan(keg_dir)

    # Serial: two symlinks in one directory would race on the restore
    symlink_n = 0
    for link in symlinks:
        symlink_n += relocate_symlink(Path(link), subs)

    to_sign: list[Path] = []
    discovered: list[str] = []
    elf_n = 0
    if regular:
        executor = ThreadPoolExecutor(max_workers=_RELOCATE_WORKERS)
        futures = [
            executor.submit(
                _process_file, p, subs, keg_root, allowed_text, skip_relocation
            )
            for p in regular
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

    # Resign all rewritten Mach-O files in a single batch (empty/no-op on Linux)
    _codesign(to_sign)

    # The manifest list is authoritative for the receipt
    changed = sorted(text_files) if text_files is not None else sorted(discovered)

    return RelocationResult(changed, len(to_sign), symlink_n, elf_n)
