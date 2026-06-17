"""Relocate a staged Homebrew bottle keg into a target prefix."""

from __future__ import annotations

import contextlib
import mmap
import os
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from brewery.core.errors import RelocationError

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

# Bounded thread pool for the regular-file relocation phase
_RELOCATE_WORKERS = min(8, os.cpu_count() or 4)


class NameKind(Enum):
    """Represents the kind of a Mach-O name."""

    ID = "id"  # LC_ID_DYLIB        -> install_name_tool -id NEW
    DYLIB = "dylib"  # LC_LOAD_*_DYLIB    -> install_name_tool -change OLD NEW
    RPATH = "rpath"  # LC_RPATH           -> install_name_tool -rpath OLD NEW


class _Kind(Enum):
    """Internal file classification for the fused relocation path."""

    MACHO = "macho"
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


def _run(cmd: list[str]) -> None:
    """Run install_name_tool / codesign synchronously.

    Args:
        cmd: The command to run.

    Raises:
        subprocess.CalledProcessError: If the command exits non-zero.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
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


def _run_macho_tools(path: Path, args: list[str]) -> None:
    """Rewrite install names and ad-hoc re-sign one Mach-O file.

    Args:
        path: The path to the Mach-O file.
        args: A non-empty install_name_tool argument list.

    Raises:
        RelocationError: If install_name_tool or codesign fails.
    """
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


def relocate_macho(path: Path, subs: dict[bytes, bytes]) -> bool:
    """Rewrite placeholder install names in one Mach-O file and re-sign.

    This is a thin wrapper kept for API/test compatibility; the keg walker
    uses the fused single-mmap path in `_process_file` instead.

    Args:
        path: The path to the Mach-O file.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        True if the file was modified, False otherwise.

    Raises:
        RelocationError: If the file could not be relocated.
    """
    args = _build_macho_args(find_install_names(path), subs)
    if not args:
        return False

    _run_macho_tools(path, args)

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
    """Rewrite a symlink whose target contains a placeholder.

    Args:
        link: The path to the symlink.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        True if the symlink was modified, False otherwise.
    """
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

    return _Kind.TEXT


def _process_file(
    path_str: str,
    subs: dict[bytes, bytes],
    keg_root: str,
    allowed_text: frozenset[str] | None,
) -> tuple[bool, str | None]:
    """Relocate one regular (non-symlink) file via a single mmap.

    Args:
        path_str: The path to the file (str; Path is deferred to here).
        subs: A mapping of placeholder bytes to their replacements.
        keg_root: The keg directory as a string, for computing relative paths.
        allowed_text: The manifest's changed_files set (relative POSIX), or None
            to substitute any marker-bearing text file.

    Returns:
        (macho_modified, text_rel): `macho_modified` True if a Mach-O file was
        rewritten; `text_rel` the relative POSIX path if a text file was
        substituted, else None.

    Raises:
        RelocationError: If the file could not be relocated.
    """
    path = Path(path_str)
    macho_args: list[str] | None = None
    new_text: bytes | None = None
    text_rel: str | None = None

    try:
        with path.open("rb") as fh:
            if os.fstat(fh.fileno()).st_size == 0:
                return False, None

            with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                # No parse and no full-file read for marker-free files
                if mm.find(_PLACEHOLDER_MARKER) == -1:
                    return False, None

                kind = _classify(mm)
                if kind is _Kind.ARCHIVE:
                    # Length-changing text substitution would corrupt headers and offsets
                    raise RelocationError(path, "static archive contains a placeholder")

                if kind is _Kind.MACHO:
                    macho_args = _build_macho_args(_collect_names(mm), subs)

                else:
                    rel = path.relative_to(keg_root).as_posix()
                    # In manifest mode, only substitute files brew listed
                    if allowed_text is None or rel in allowed_text:
                        new = _apply(bytes(mm), subs)
                        if new != bytes(mm):
                            new_text = new
                            text_rel = rel

    except OSError as exc:
        raise RelocationError(path, f"read failed: {exc}") from exc

    # Mapping is closed here, so safe to mutate the file
    if macho_args is not None:
        if not macho_args:
            return False, None  # Marker present but not in any install name
        _run_macho_tools(path, macho_args)
        return True, None

    if new_text is None:
        return False, None

    with _writable(path):
        path.write_bytes(new_text)

    return False, text_rel


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
    being `:any_skip_relocation` - when true this is a no-op.

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
    if skip_relocation:
        return RelocationResult([], 0, 0)

    subs = build_substitutions(prefix, cellar, repository, extra=extra_tokens)
    keg_root = str(keg_dir)

    allowed_text: frozenset[str] | None = None
    if text_files is not None:
        allowed_text = frozenset(text_files)
        # Fail fast on a manifest/extract mismatch.
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

    macho_n = 0
    discovered: list[str] = []
    if regular:
        executor = ThreadPoolExecutor(max_workers=_RELOCATE_WORKERS)
        futures = [
            executor.submit(_process_file, p, subs, keg_root, allowed_text)
            for p in regular
        ]
        try:
            for fut in as_completed(futures):
                macho_mod, text_rel = (
                    fut.result()
                )  # First RelocationError re-raises here
                if macho_mod:
                    macho_n += 1

                elif text_rel is not None:
                    discovered.append(text_rel)

        except BaseException:
            # Cancel queued tasks
            executor.shutdown(cancel_futures=True)
            raise

        else:
            executor.shutdown()

    # The manifest list is authoritative for the receipt
    changed = sorted(text_files) if text_files is not None else sorted(discovered)

    return RelocationResult(changed, macho_n, symlink_n)
