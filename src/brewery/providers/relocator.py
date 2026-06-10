"""Relocate a staged Homebrew bottle keg into a target prefix."""

from __future__ import annotations

import contextlib
import mmap
import os
import struct
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from brewery.core.errors import BrewError

# Mach-O Constants
_MH_MAGIC = 0xFEEDFACE  # 32-bit, host byte order
_MH_MAGIC_64 = 0xFEEDFACF  # 64-bit, host byte order
_MH_CIGAM = 0xCEFAEDFE  # 32-bit, swapped
_MH_CIGAM_64 = 0xCFFAEDFE  # 64-bit, swapped

_FAT_MAGIC = 0xCAFEBABE  # Fat header, big-endian
_FAT_CIGAM = 0xBEBAFECA
_FAT_MAGIC_64 = 0xCAFEBABF
_FAT_CIGAM_64 = 0xBFBAFECA

_LC_REQ_DYLD = 0x80000000
_LC_ID_DYLIB = 0x0D
_LC_LOAD_DYLIB = 0x0C
_LC_LOAD_WEAK_DYLIB = 0x18 | _LC_REQ_DYLD
_LC_REEXPORT_DYLIB = 0x1F | _LC_REQ_DYLD
_LC_LAZY_LOAD_DYLIB = 0x20
_LC_LOAD_UPWARD_DYLIB = 0x23 | _LC_REQ_DYLD
_LC_RPATH = 0x1C | _LC_REQ_DYLD

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

_PLACEHOLDER_MARKER = b"@@HOMEBREW_"


class NameKind(Enum):
    ID = "id"  # LC_ID_DYLIB        -> install_name_tool -id NEW
    DYLIB = "dylib"  # LC_LOAD_*_DYLIB    -> install_name_tool -change OLD NEW
    RPATH = "rpath"  # LC_RPATH           -> install_name_tool -rpath OLD NEW


@dataclass(frozen=True)
class InstallName:
    """Represents an install name in a Mach-O file."""

    kind: NameKind
    value: str


class RelocationError(BrewError):
    """Raised when a keg cannot be relocated natively.

    Used as a per-formula signal to fallback to brew.
    """

    def __init__(self, path: Path, reason: str) -> None:
        """Initialise a RelocationError.

        Args:
            path: The path to the file that could not be relocated.
            reason: The reason for the relocation failure.
        """
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


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
    return magic in {
        _MH_MAGIC,
        _MH_MAGIC_64,
        _MH_CIGAM,
        _MH_CIGAM_64,
        _FAT_MAGIC,
        _FAT_CIGAM,
        _FAT_MAGIC_64,
        _FAT_CIGAM_64,
    }


def _read_cstr(data: memoryview, start: int, end: int) -> bytes:
    """Read a null-terminated string from a memoryview.

    Args:
        data: The memoryview to read from.
        start: The start index (inclusive).
        end: The end index (exclusive).

    Returns:
        The null-terminated string as bytes.
    """
    nul = bytes(data[start:end]).find(b"\x00")

    return bytes(data[start : start + nul]) if nul != -1 else bytes(data[start:end])


def _parse_thin(data: memoryview, base: int) -> list[InstallName]:
    """Parse one thin Mach-O slice starting at `base`.

    Args:
        data: The memoryview to read from.
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


def find_install_names(path: Path) -> list[InstallName]:
    """Return every dylib/rpath install name in a Mach-O (handles fat binaries).

    Args:
        path: The path to the Mach-O file.

    Returns:
        A list of InstallName objects found in the file.
    """
    with path.open("rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size == 0:
            return []

        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            data = memoryview(mm)
            try:
                raw_magic = struct.unpack_from(">I", data, 0)[0]

                if raw_magic in (_FAT_MAGIC, _FAT_CIGAM, _FAT_MAGIC_64, _FAT_CIGAM_64):
                    is64 = raw_magic in (_FAT_MAGIC_64, _FAT_CIGAM_64)
                    nfat = struct.unpack_from(">I", data, 4)[0]  # fat header is BE
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

            finally:
                data.release()


def _run(cmd: list[str]) -> None:
    """Subprocess boundary delegates to `run_capture` so tests can mock
    install_name_tool / codesign.

    Args:
        cmd: The command to run.
    """
    import asyncio

    from brewery.core.shell import run_capture

    out, err, code = asyncio.run(run_capture(*cmd))
    if code != 0:
        raise subprocess.CalledProcessError(code, cmd, out, err)


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


def relocate_macho(path: Path, subs: dict[bytes, bytes]) -> bool:
    """Rewrite placeholder install names in one Mach-O file and re-sign.

    Returns True if the file was modified. Raises RelocationError if
    install_name_tool overflows the header pad (caller falls back to brew).

    Args:
        path: The path to the Mach-O file.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        True if the file was modified, False otherwise.

    Raises:
        RelocationError: If the file could not be relocated.
    """
    names = find_install_names(path)
    args: list[str] = []
    for name in names:
        old = name.value
        new = _apply(old.encode("utf-8", "surrogateescape"), subs)
        if new == old.encode("utf-8", "surrogateescape"):
            continue  # No placeholder in this entry

        new_s = new.decode("utf-8", "surrogateescape")
        if name.kind is NameKind.ID:
            args += ["-id", new_s]

        elif name.kind is NameKind.RPATH:
            args += ["-rpath", old, new_s]

        else:
            args += ["-change", old, new_s]

    if not args:
        return False

    with _writable(path):
        try:
            _run(["install_name_tool", *args, str(path)])
        except subprocess.CalledProcessError as exc:
            # Most likely cause: header pad exhausted (load command too large)
            raise RelocationError(
                path, f"install_name_tool failed: {exc.stderr.strip()}"
            )

        # install_name_tool invalidates the code signature, so ad-hoc re-sign
        try:
            _run(
                [
                    "codesign",
                    "--force",
                    "--sign",
                    "-",
                    "--preserve-metadata=entitlements,flags,runtime",
                    str(path),
                ]
            )
        except subprocess.CalledProcessError as exc:
            raise RelocationError(path, f"codesign failed: {exc.stderr.strip()}")

    return True


def relocate_text(path: Path, subs: dict[bytes, bytes]) -> bool:
    """Substitute placeholders in a text/script/config file. Returns True if
    the file was modified. Length changes are fine (file is rewritten).

    Args:
        path: The path to the text file.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        True if the file was modified, False otherwise.
    """
    data = path.read_bytes()
    if _PLACEHOLDER_MARKER not in data:
        return False

    new = _apply(data, subs)
    if new == data:
        return False

    with _writable(path):
        path.write_bytes(new)

    return True


def relocate_symlink(link: Path, subs: dict[bytes, bytes]) -> bool:
    """Rewrite a symlink whose target contains a placeholder."""
    target = os.readlink(link).encode("utf-8", "surrogateescape")
    if _PLACEHOLDER_MARKER not in target:
        return False

    new = _apply(target, subs)
    if new == target:
        return False

    with _writable(link.parent):
        link.unlink()
        os.symlink(new.decode("utf-8", "surrogateescape"), link)

    return True


def relocate_keg(
    keg_dir: Path,
    *,
    prefix: Path,
    cellar: Path,
    repository: Path,
    skip_relocation: bool = False,
    extra_tokens: dict[str, str] | None = None,
) -> int:
    """Relocate an extracted keg in place.

    `skip_relocation` should be set from the catalog bottle's `cellar` value
    being `:any_skip_relocation` - when true this is a no-op.

    Args:
        keg_dir: The path to the keg directory.
        prefix: The new prefix to use.
        cellar: The new cellar path.
        repository: The new repository path.
        skip_relocation: Whether to skip relocation.
        extra_tokens: Any extra tokens to use for substitution.

    Returns:
        The number of files modified.

    Raises:
        RelocationError: If the relocation fails.
    """
    if skip_relocation:
        return 0

    subs = build_substitutions(prefix, cellar, repository, extra=extra_tokens)
    modified = 0

    for root, _dirs, files in os.walk(keg_dir):
        for fname in files:
            fpath = Path(root) / fname
            if fpath.is_symlink():
                modified += relocate_symlink(fpath, subs)
                continue

            if is_macho(fpath):
                modified += relocate_macho(fpath, subs)

            else:
                modified += relocate_text(fpath, subs)

    return modified
