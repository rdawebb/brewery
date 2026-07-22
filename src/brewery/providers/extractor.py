"""Extract a downloaded bottle tarball into a staging directory."""

from __future__ import annotations

import tarfile
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


_TarFilter = Callable[[tarfile.TarInfo, str], tarfile.TarInfo]


def _keg_filter(archive: Path | None = None) -> _TarFilter:
    """Build the per-member extraction filter for one archive.

    The filter applies the data-filter security checks, but preserves the
    member's exact mode, permits the symlinks brew creates verbatim, and
    rejects the archive before anything is written once it exceeds the
    decompression-bomb ceilings.

    Args:
        archive: The archive being extracted, for error context.

    Returns:
        A filter callable for `tarfile.extractall`, holding the running totals.
    """
    totals = {"bytes": 0, "members": 0}

    def _filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo:
        """Apply the data-filter security checks and track the running totals.

        Args:
            member: The tar member being filtered.
            dest_path: The destination path for the member.

        Returns:
            The filtered tar member.
        """
        totals["bytes"] += member.size
        totals["members"] += 1

        if totals["bytes"] > _MAX_EXTRACTED_BYTES:
            raise ExtractionError(
                f"bottle expands past the {_MAX_EXTRACTED_BYTES} byte extraction "
                f"limit at member {member.name!r}",
                archive=archive,
            )

        if totals["members"] > _MAX_MEMBERS:
            raise ExtractionError(
                f"bottle holds more than {_MAX_MEMBERS} members",
                archive=archive,
            )

        try:
            safe = tarfile.data_filter(member, dest_path)  # FilterError if unsafe

        except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
            if member.issym():
                return member.replace(mode=member.mode & 0o777, deep=False)
            raise

        # Keep the bottle's real permission bits (read-only files stay read-only)
        return safe.replace(mode=member.mode & 0o777, deep=False)

    return _filter


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
        tar.extractall(dest, filter=tar_filter)


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
    tar_filter = _keg_filter(archive=archive)

    try:
        if fmt == "gzip":
            with archive.open("rb") as fh:
                _extract_stream(fh, dest, tar_filter)

        else:  # zstd
            dctx = zstandard.ZstdDecompressor()
            with archive.open("rb") as fh, dctx.stream_reader(fh) as reader:
                _extract_stream(reader, dest, tar_filter)

    except tarfile.FilterError as exc:
        raise ExtractionError(
            f"unsafe tar member in {archive.name}: {exc}", archive=archive, dest=dest
        ) from exc

    except (tarfile.TarError, zstandard.ZstdError, OSError) as exc:
        raise ExtractionError(
            f"failed to extract {archive.name}: {exc}", archive=archive, dest=dest
        ) from exc

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
            f"expected a single top-level keg dir, found {[p.name for p in top]}",
            dest=dest,
        )

    name_dir = top[0]
    versions = [
        p for p in name_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ]

    if len(versions) != 1:
        raise ExtractionError(
            f"expected one version dir under {name_dir.name}, "
            f"found {[p.name for p in versions]}",
            dest=dest,
        )

    return versions[0]
