"""Extract a downloaded bottle tarball into a staging directory."""

from __future__ import annotations

import os
import tarfile
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import zstandard

from brewery.core.errors import ExtractionError

_GZIP_MAGIC = b"\x1f\x8b"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Decompression-bomb ceilings: bottles are sha256-pinned to the catalog feed, so
# these only bite on a hostile or corrupt feed; the caps are set above any real
# keg, so they never fire in normal use. In streaming mode tarfile reads exactly
# the header-declared byte count per member, so summing `member.size` bounds the
# decompressed stream.
_MAX_EXTRACTED_BYTES = 17_179_869_184  # 16 GiB
_MAX_MEMBERS = 500_000


def detect_format(archive: Path) -> str:
    """Return 'gzip' or 'zstd' from the file's magic bytes.

    Args:
        archive: The path to the archive file.

    Returns:
        The compression format of the archive.
    """
    with archive.open("rb") as fh:
        head = fh.read(4)

    if head[:2] == _GZIP_MAGIC:
        return "gzip"

    if head[:4] == _ZSTD_MAGIC:
        return "zstd"

    raise ExtractionError(
        f"unrecognized bottle compression: {head[:4].hex()}", archive=archive
    )


# A filter may return None to tell tarfile to skip the member entirely
_TarFilter = Callable[[tarfile.TarInfo, str], tarfile.TarInfo | None]

# Link member the filter deferred: (name, link target, is a symlink)
_DeferredLink = tuple[str, str, bool]

# The function pattern used to create symlinks during extraction
_MakeLink = Callable[[str, Path], None]


def _contained(name: str) -> str | None:
    """Normalise a tar member name, or reject it for leaving the root.

    tarfile joins the member name onto the destination directory verbatim, so
    containment is decidable from the string alone as long as the destination
    holds no symlinks.

    Args:
        name: The raw member name from the tar header.

    Returns:
        The normalised root-relative name; empty if the member names the
        destination root itself, or None if the name is not safely contained.
    """
    if "\x00" in name:
        # Paths with NUL bytes are rejected to avoid tarfile's ValueError
        return None

    # Leading slashes are stripped
    parts = [part for part in name.lstrip("/").split("/") if part and part != "."]
    if ".." in parts:
        return None

    return "/".join(parts)


def _fold(name: str) -> str:
    """Return `name` in the form the filesystem compares paths in.

    Handles default macOS volumes being case-insensitive and Unicode
    normalisation-insensitive. On a case-sensitive volume it can only reject an
    archive that nests two differently-cased names, which no bottle does.

    Args:
        name: A destination-relative member name.

    Returns:
        A folded key, for comparison only -- never for building a path.
    """
    if name.isascii():
        return name.lower()

    return unicodedata.normalize("NFC", name).casefold()


def _under_symlink(folded: str, symlinks: set[str]) -> bool:
    """Whether an ancestor directory of `folded` was created as a symlink.

    Args:
        folded: The folded member name to check, from `_fold`.
        symlinks: Folded names this archive has already created as symlinks.

    Returns:
        True if `folded` sits under one of `symlinks`.
    """
    head = folded
    while head := head.rpartition("/")[0]:
        if head in symlinks:
            return True

    return False


def _keg_filter(
    archive: Path | None = None,
) -> tuple[_TarFilter, list[_DeferredLink]]:
    """Build the per-member extraction filter for one archive.

    The filter keeps the member inside the destination, preserves the member's
    exact mode, and rejects the archive before anything is written once it
    exceeds the decompression-bomb ceilings.

    Args:
        archive: The archive being extracted, for error context.

    Returns:
        A filter callable for `tarfile.extractall`, holding the running totals,
        and the list it appends deferred link members to in stream order.
    """
    total_bytes = 0
    total_members = 0
    deferred: list[_DeferredLink] = []

    def _filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
        """Apply the containment checks and track the running totals.

        Args:
            member: The tar member being filtered.
            dest_path: The destination path for the member.

        Returns:
            The filtered tar member, or None to skip it.
        """
        nonlocal total_bytes, total_members
        total_bytes += member.size
        total_members += 1

        if total_bytes > _MAX_EXTRACTED_BYTES:
            raise ExtractionError(
                f"bottle expands past the {_MAX_EXTRACTED_BYTES} byte extraction "
                f"limit at member {member.name!r}",
                archive=archive,
            )

        if total_members > _MAX_MEMBERS:
            raise ExtractionError(
                f"bottle holds more than {_MAX_MEMBERS} members",
                archive=archive,
            )

        # A keg is files, directories and links; raise for device nodes and fifos
        if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
            raise tarfile.SpecialFileError(member)

        name = _contained(member.name)
        if name is None:
            raise tarfile.OutsideDestinationError(member, member.name)

        if not name:
            # Reject the root directory itself
            if member.isdir():
                return None

            raise tarfile.OutsideDestinationError(member, member.name)

        if member.issym():
            # Targets are left verbatim, relocator rewrites them later
            deferred.append((name, member.linkname, True))
            return None

        if member.islnk():
            # Leading slash indicates an absolute link target, not a member of the archive
            if member.linkname.startswith("/"):
                raise tarfile.AbsoluteLinkError(member)

            # Target otherwise needs the same treatment as the member name
            target = _contained(member.linkname)
            if not target:
                raise tarfile.LinkOutsideDestinationError(member, member.linkname)

            deferred.append((name, target, False))
            return None

        # Mutated in place rather than via TarInfo.replace()
        member.name = name

        # Set to None so tarfile chown does nothing
        member.uid = None  # ty: ignore[invalid-assignment]
        member.gid = None  # ty: ignore[invalid-assignment]
        member.uname = None  # ty: ignore[invalid-assignment]
        member.gname = None  # ty: ignore[invalid-assignment]

        if member.mode is not None:
            # Keep the bottle's real permission bits
            member.mode &= 0o777

        return member

    return _filter, deferred


def _link_in_readonly_parent(
    make: _MakeLink,
    src: str,
    path: Path,
    *,
    archive: Path | None = None,
    dest: Path | None = None,
) -> None:
    """Create a link inside a directory whose archive mode denies writes.

    `extractall` applies each directory's mode from the archive, so a member
    with a read-only mode is already locked by the time its deferred links are
    created; this grants the owner write permissions, creates the link, then
    restores the recorded mode.

    Args:
        make: `os.symlink` or `os.link`.
        src: The link target.
        path: The entry to create.
        archive: The archive being extracted, for error context.
        dest: The staging directory, for error context.
    """
    parent = path.parent
    mode = parent.stat().st_mode & 0o7777

    try:
        parent.chmod(mode | 0o300)

    except PermissionError as exc:
        raise ExtractionError(
            f"staging directory {parent} is not writable and is not owned by "
            f"this user, so link {path.name!r} cannot be created",
            archive=archive,
            dest=dest,
        ) from exc

    try:
        make(src, path)

    finally:
        parent.chmod(mode)


def _make_link(
    make: _MakeLink,
    src: str,
    path: Path,
    *,
    archive: Path | None = None,
    dest: Path | None = None,
) -> None:
    """Create one deferred link, handling each failure where it lands.

    Args:
        make: `os.symlink` or `os.link`.
        src: The link target, as `make` expects it.
        path: The entry to create.
        archive: The archive being extracted, for error context.
        dest: The staging directory, for error context.
    """
    try:
        make(src, path)
        return

    except FileExistsError as exc:
        # Link target already exists; replace it if it's not a symlink
        if not path.is_symlink():
            raise ExtractionError(
                f"unsafe tar member: link {path.name!r} would replace an "
                f"extracted entry",
                archive=archive,
                dest=dest,
            ) from exc

        path.unlink()

    except FileNotFoundError as exc:
        # Parent is missing or a hard link names a target the archive never wrote
        if path.parent.is_dir():
            raise ExtractionError(
                f"broken hard link: {src}",
                archive=archive,
                dest=dest,
            ) from exc

        path.parent.mkdir(parents=True, exist_ok=True)

    except PermissionError:
        _link_in_readonly_parent(make, src, path, archive=archive, dest=dest)
        return

    make(src, path)


def _apply_links(
    dest: Path, deferred: list[_DeferredLink], archive: Path | None = None
) -> None:
    """Create the archive's link members once every file and directory is on disk.

    Walks `deferred` in stream order, so a hard link's target is always either a
    regular file `extractall` already wrote or a symlink earlier in this list.

    Args:
        dest: The staging directory the archive was extracted into.
        deferred: The link members `_keg_filter` held back, in stream order.
        archive: The archive being extracted, for error context.
    """
    symlinks: set[str] = set()

    for name, linkname, is_sym in deferred:
        folded = _fold(name)
        path = dest / name

        if is_sym:
            if _under_symlink(folded, symlinks):
                raise ExtractionError(
                    f"unsafe tar member: symlink {name!r} sits under an "
                    f"earlier symlink",
                    archive=archive,
                    dest=dest,
                )

            _make_link(os.symlink, linkname, path, archive=archive, dest=dest)
            symlinks.add(folded)
            continue

        folded_target = _fold(linkname)
        if folded in symlinks or _under_symlink(folded, symlinks):
            raise ExtractionError(
                f"unsafe tar member: hard link {name!r} sits at or under an "
                f"earlier symlink",
                archive=archive,
                dest=dest,
            )

        if folded_target in symlinks or _under_symlink(folded_target, symlinks):
            raise ExtractionError(
                f"unsafe tar member: hard link {name!r} aims at or through the "
                f"symlink {linkname!r}",
                archive=archive,
                dest=dest,
            )

        _make_link(os.link, str(dest / linkname), path, archive=archive, dest=dest)


def _extract_stream(fileobj: BinaryIO, dest: Path, tar_filter: _TarFilter) -> None:
    """Extract a tar archive from a file-like object into a directory.

    Args:
        fileobj: The file-like object to read the archive from.
        dest: The directory to extract the archive into.
        tar_filter: The per-member filter to apply.
    """
    # 'r|*' auto-detects gzip vs uncompressed in streaming mode; the zstd path
    # passes an already-decompressed (raw) tar stream, which reads as
    # uncompressed; the gzip path is decompressed here
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        tar.extractall(str(dest), filter=tar_filter)


def extract_bottle(archive: Path, dest: Path) -> Path:
    """Extract `archive` into `dest` and return the keg directory.

    A bottle unpacks to `<name>/<version>/...` (plus a `<name>/.brew`
    metadata dir). The returned path is that `<name>/<version>` keg root,
    which is what the relocator operates on.

    Args:
        archive: The path to the archive file.
        dest: The directory to extract the archive into.

    Returns:
        The path to the extracted keg directory.
    """
    fmt = detect_format(archive)
    dest.mkdir(parents=True, exist_ok=True)
    tar_filter, deferred = _keg_filter(archive=archive)

    try:
        if fmt == "gzip":
            with archive.open("rb") as fh:
                _extract_stream(fh, dest, tar_filter)

        else:  # zstd
            dctx = zstandard.ZstdDecompressor()
            with archive.open("rb") as fh, dctx.stream_reader(fh) as reader:
                _extract_stream(reader, dest, tar_filter)

        _apply_links(dest, deferred, archive=archive)

    except tarfile.FilterError as exc:
        raise ExtractionError(
            f"unsafe tar member in {archive.name}: {exc}", archive=archive, dest=dest
        ) from exc

    except (tarfile.TarError, zstandard.ZstdError, OSError) as exc:
        raise ExtractionError(
            f"failed to extract {archive.name}: {exc}", archive=archive, dest=dest
        ) from exc

    return _locate_keg(dest)


def _real_dir(path: Path) -> bool:
    """Whether `path` is a directory rather than a symlink to one.

    Args:
        path: The path to test.

    Returns:
        True if the path is a directory and not a symlink.
    """
    return path.is_dir() and not path.is_symlink()


def _locate_keg(dest: Path) -> Path:
    """Resolve <dest>/<name>/<version>, ignoring the .brew metadata dir.

    Args:
        dest: The directory to search for the keg.

    Returns:
        The path to the resolved keg directory.
    """
    top = [p for p in dest.iterdir() if _real_dir(p) and not p.name.startswith(".")]
    if len(top) != 1:
        raise ExtractionError(
            f"expected a single top-level keg dir, found {[p.name for p in top]}",
            dest=dest,
        )

    name_dir = top[0]
    versions = [
        p for p in name_dir.iterdir() if _real_dir(p) and not p.name.startswith(".")
    ]

    if len(versions) != 1:
        raise ExtractionError(
            f"expected one version dir under {name_dir.name}, "
            f"found {[p.name for p in versions]}",
            dest=dest,
        )

    return versions[0]
