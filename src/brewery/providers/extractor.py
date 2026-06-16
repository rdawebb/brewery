"""Extract a downloaded bottle tarball into a staging directory."""

from __future__ import annotations

import tarfile
from pathlib import Path
from typing import BinaryIO

import zstandard

from brewery.core.errors import BrewError

_GZIP_MAGIC = b"\x1f\x8b"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class ExtractionError(BrewError):
    """A bottle could not be extracted (bad format, unsafe member, corrupt
    archive, or unexpected layout). Treated as a per-formula fallback signal."""


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

    raise ExtractionError(f"unrecognized bottle compression: {head[:4].hex()}")


def _keg_filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo:
    """data-filter security checks, but preserve the member's exact mode, and permit
    the symlinks brew creates verbatim.

    Args:
        member: The tarfile member to filter.
        dest_path: The destination path for the extracted files.

    Returns:
        The filtered tarfile member.
    """
    try:
        safe = tarfile.data_filter(member, dest_path)  # Raises FilterError if unsafe

    except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
        if member.issym():
            return member.replace(mode=member.mode & 0o777, deep=False)
        raise

    # Keep the bottle's real permission bits (read-only files stay read-only)
    return safe.replace(mode=member.mode & 0o777, deep=False)


def _extract_stream(fileobj: BinaryIO, dest: Path) -> None:
    """Extract a tar archive from a file-like object into a directory.

    Args:
        fileobj: The file-like object to read the archive from.
        dest: The directory to extract the archive into.
    """
    # 'r|*' auto-detects gzip vs uncompressed in streaming mode. The zstd path
    # passes an already-decompressed (raw) tar stream, which reads as
    # uncompressed; the gzip path is decompressed here.
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        tar.extractall(dest, filter=_keg_filter)


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

    try:
        if fmt == "gzip":
            with archive.open("rb") as fh:
                _extract_stream(fh, dest)

        else:  # zstd
            dctx = zstandard.ZstdDecompressor()
            with archive.open("rb") as fh, dctx.stream_reader(fh) as reader:
                _extract_stream(reader, dest)

    except tarfile.FilterError as exc:
        raise ExtractionError(f"unsafe tar member in {archive.name}: {exc}") from exc

    except (tarfile.TarError, zstandard.ZstdError, OSError) as exc:
        raise ExtractionError(f"failed to extract {archive.name}: {exc}") from exc

    return _locate_keg(dest)


def _locate_keg(dest: Path) -> Path:
    """Resolve <dest>/<name>/<version>, ignoring the .brew metadata dir.

    Args:
        dest: The directory to search for the keg.

    Returns:
        The path to the resolved keg directory.
    """
    top = [p for p in dest.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(top) != 1:
        raise ExtractionError(
            f"expected a single top-level keg dir, found {[p.name for p in top]}"
        )

    name_dir = top[0]
    versions = [
        p for p in name_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ]

    if len(versions) != 1:
        raise ExtractionError(
            f"expected one version dir under {name_dir.name}, "
            f"found {[p.name for p in versions]}"
        )

    return versions[0]
