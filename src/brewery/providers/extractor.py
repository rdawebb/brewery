"""Extract a downloaded bottle tarball into a staging directory."""

from __future__ import annotations

import os
import tarfile
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

import zstandard

from brewery.core.errors import ExtractionError

_GZIP_MAGIC = b"\x1f\x8b"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# Permission bits kept from a member header; setuid/setgid/sticky are dropped
_MODE_MASK = 0o777

# Mode `tarfile` creates directories with before its final fixup pass
_SCRATCH_MODE = 0o700

# Bytes moved per read when copying a member body, matching tarfile's default
_COPY_BUF = 16 * 1024  # 16 KiB

# Decompression-bomb ceilings, set well above any real keg: bottles are
# sha256-pinned, so these only bite on a hostile or corrupt feed; streaming
# tarfile reads exactly `member.size` per member, so the sum bounds the stream
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


# Link member deferred by the member loop (name, link target, is a symlink)
_DeferredLink = tuple[str, str, bool]


class _Readable(Protocol):
    """The only thing the copy loop needs from `TarFile.fileobj`."""

    def read(self, size: int, /) -> bytes:
        """Read up to `size` bytes from the stream.

        Args:
            size: The maximum number of bytes to read.

        Returns:
            The bytes read from the stream."""
        ...


class _Sink(Protocol):
    """A relocator driven from the member loop.

    `relocator.StreamRelocator` is the reference implementation and documents
    what each hook does with its argument; only what the member loop guarantees
    is stated here.
    """

    # How many bytes off the front of a body `member` wants to see
    head_bytes: int

    def member(self, name: str, path: str, head: bytes, size: int) -> bool:
        """Offer a regular file member; True to route its body through `file`.

        `head` is `head_bytes` long, or the whole body if that is shorter.
        """
        ...

    def file(self, data: bytes) -> bytes:
        """Rewrite a whole buffered body. Called once per True from `member`."""
        ...

    def link(self, name: str, path: str, target: str) -> str:
        """Rewrite a symlink target before the link is created."""
        ...

    def hardlink(self, name: str, path: str, target: str) -> None:
        """Offer a hard-link member; `target` is a member name, not a path."""
        ...

    def defer(self, name: str, path: str) -> None:
        """Offer a member the loop wrote without ever showing a head."""
        ...


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


class _Extractor:
    """Drives one archive's member loop, holding the state `extractall` kept.

    Link members are still deferred to `_apply_links`, so a hard link's target
    is always on disk by the time it is created.
    """

    __slots__ = (
        "_bytes",
        "_dirs",
        "_made",
        "_members",
        "_root",
        "archive",
        "deferred",
        "dest",
        "sink",
    )

    def __init__(
        self, dest: Path, archive: Path | None = None, sink: _Sink | None = None
    ) -> None:
        """Prepare the per-archive extraction state.

        Args:
            dest: The directory to extract the archive into.
            archive: The archive being extracted, for error context.
            sink: A relocator to substitute placeholders as members are written,
                or None to write the archive's bytes verbatim.
        """
        self.dest = dest
        self.archive = archive
        self.sink = sink
        self.deferred: list[_DeferredLink] = []
        self._bytes = 0
        self._members = 0

        # Root directory for the archive, used to normalise member names
        self._root = f"{dest}/"

        # Directory members, for the reversed mode/mtime pass
        self._dirs: list[tuple[str, int | None, float]] = []

        # Directory names already on disk
        self._made: set[str] = set()

    def _admit(self, member: tarfile.TarInfo) -> str | None:
        """Screen one member, returning the destination-relative name to write.

        Keeps the member inside the destination, rejects the archive once it
        exceeds the decompression-bomb ceilings, and holds link members back for
        `_apply_links`.

        Args:
            member: The tar member being screened.

        Returns:
            The normalised root-relative name, or None if there is nothing to write.

        Raises:
            ExtractionError: If a bomb ceiling is exceeded.
            tarfile.FilterError: If the member is unsafe.
        """
        self._bytes += member.size
        self._members += 1

        if self._bytes > _MAX_EXTRACTED_BYTES:
            raise ExtractionError(
                f"bottle expands past the {_MAX_EXTRACTED_BYTES} byte extraction "
                f"limit at member {member.name!r}",
                archive=self.archive,
            )

        if self._members > _MAX_MEMBERS:
            raise ExtractionError(
                f"bottle holds more than {_MAX_MEMBERS} members",
                archive=self.archive,
            )

        # A keg is files, directories and links; raise for device nodes and fifos
        if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
            raise tarfile.SpecialFileError(member)

        name = _contained(member.name)
        if name is None:
            raise tarfile.OutsideDestinationError(member, member.name)

        if not name:
            # Skip the root directory itself, rather than applying its mode to
            # the staging directory; anything else naming the root is unsafe
            if member.isdir():
                return None

            raise tarfile.OutsideDestinationError(member, member.name)

        if member.issym():
            # Targets are left verbatim; the sink or the relocator rewrites them
            self.deferred.append((name, member.linkname, True))
            return None

        if member.islnk():
            # Leading slash indicates an absolute link target, not a member of the archive
            if member.linkname.startswith("/"):
                raise tarfile.AbsoluteLinkError(member)

            # Target otherwise needs the same treatment as the member name
            target = _contained(member.linkname)
            if not target:
                raise tarfile.LinkOutsideDestinationError(member, member.linkname)

            self.deferred.append((name, target, False))
            return None

        return name

    def _parent(self, name: str) -> None:
        """Create the member's parent directory if the archive declared none.

        Args:
            name: The destination-relative member name.
        """
        head = name.rpartition("/")[0]
        if head and head not in self._made:
            # Default permissions, so the final directory member can reset it
            os.makedirs(self._root + head, exist_ok=True)
            self._made.add(head)

    def _directory(self, member: tarfile.TarInfo, name: str) -> None:
        """Create a directory member, deferring its mode to the final pass.

        Args:
            member: The directory member.
            name: The destination-relative member name.
        """
        self._parent(name)
        path = self._root + name

        try:
            os.mkdir(path, _SCRATCH_MODE)

        except FileExistsError:
            if not os.path.isdir(path):
                raise

        self._made.add(name)
        self._dirs.append((name, member.mode, member.mtime))

    def _chunks(self, src: _Readable, size: int, name: str) -> Iterator[bytes]:
        """Yield exactly `size` bytes off the tar stream, in read-sized pieces.

        The one place a member body is pulled off the stream, so a short stream
        is one error rather than one per consumer.

        Args:
            src: The archive's underlying stream, positioned at the body.
            size: The byte count the member header declares.
            name: The member name, for the error message.

        Yields:
            The bytes read, in chunks of at most `_COPY_BUF`.

        Raises:
            tarfile.ReadError: If the stream runs out first.
        """
        while size > 0:
            chunk = src.read(min(size, _COPY_BUF))
            if not chunk:
                raise tarfile.ReadError(f"unexpected end of data in {name!r}")

            yield chunk
            size -= len(chunk)

    def _copy(self, src: _Readable, dst: BinaryIO, size: int, name: str) -> None:
        """Move exactly `size` bytes from the tar stream into an open file.

        Args:
            src: The archive's underlying stream, positioned at the body.
            dst: The output file.
            size: The byte count the member header declares.
            name: The member name, for the error message.

        Raises:
            tarfile.ReadError: If the stream runs out first.
        """
        dst.writelines(self._chunks(src, size, name))

    def _take(self, src: _Readable, size: int, name: str) -> bytes:
        """Read exactly `size` bytes off the tar stream into memory.

        Args:
            src: The archive's underlying stream, positioned at the body.
            size: The number of bytes wanted.
            name: The member name, for the error message.

        Returns:
            The bytes read.

        Raises:
            tarfile.ReadError: If the stream runs out first.
        """
        return b"".join(self._chunks(src, size, name))

    def _relocated(
        self, src: _Readable, dst: BinaryIO, member: tarfile.TarInfo, name: str
    ) -> None:
        """Write one member's body past the sink.

        Small text files are buffered whole and substituted before being written;
        everything else streams straight through, with the sink recording a defer
        for `finish` if needed.

        Args:
            src: The archive's underlying stream, positioned at the body.
            dst: The output file.
            member: The file member.
            name: The destination-relative member name.
        """
        assert self.sink is not None
        size = member.size
        head = self._take(src, min(size, self.sink.head_bytes), member.name)

        if self.sink.member(name, self._root + name, head, size):
            body = head + self._take(src, size - len(head), member.name)
            dst.write(self.sink.file(body))
            return

        dst.write(head)
        self._copy(src, dst, size - len(head), member.name)

    def _regular(
        self, tar: tarfile.TarFile, member: tarfile.TarInfo, name: str
    ) -> None:
        """Write one regular file member, then apply its mode and mtime.

        Args:
            tar: The open archive, for its stream and position.
            member: The file member.
            name: The destination-relative member name.
        """
        self._parent(name)
        path = self._root + name

        source = tar.fileobj
        source.seek(member.offset_data)

        with open(path, "wb") as dst:
            if member.sparse is not None:
                # A sparse body arrives in pieces the sink cannot classify from
                # a head, so written whole and deferred to `finish`; the stub
                # types `sparse` as an int - it is (offset, size) pairs
                for offset, size in member.sparse:  # ty: ignore[not-iterable]
                    dst.seek(offset)
                    self._copy(source, dst, size, member.name)

                dst.seek(member.size)
                dst.truncate()

                if self.sink is not None:
                    self.sink.defer(name, path)

            elif self.sink is None:
                self._copy(source, dst, member.size, member.name)

            else:
                self._relocated(source, dst, member, name)

        if member.mode is not None:
            os.chmod(path, member.mode & _MODE_MASK)

        os.utime(path, (member.mtime, member.mtime))

    def extract(self, tar: tarfile.TarFile) -> None:
        """Write every member of `tar`, in the order the stream declares them.

        Args:
            tar: The archive to extract, opened in streaming mode.
        """
        for member in tar:
            name = self._admit(member)
            if name is None:
                continue

            if member.isdir():
                self._directory(member, name)

            else:
                self._regular(tar, member, name)

    def _fail(self, message: str) -> ExtractionError:
        """Build an ExtractionError carrying this archive's context.

        Args:
            message: What went wrong.

        Returns:
            The error, for the caller to raise.
        """
        return ExtractionError(message, archive=self.archive, dest=self.dest)

    def _make_link(self, make: _MakeLink, src: str, path: Path) -> None:
        """Create one deferred link, handling each failure where it lands.

        Args:
            make: `os.symlink` or `os.link`.
            src: The link target, as `make` expects it.
            path: The entry to create.

        Raises:
            ExtractionError: If the link would replace an extracted entry, or
                names a hard-link target the archive never wrote.
        """
        try:
            make(src, path)
            return

        except FileExistsError as exc:
            # Link target already exists; replace it if it's not a symlink
            if not path.is_symlink():
                raise self._fail(
                    f"unsafe tar member: link {path.name!r} would replace an "
                    f"extracted entry"
                ) from exc

            path.unlink()

        except FileNotFoundError as exc:
            # Parent missing, or a hard link names a target never written
            if path.parent.is_dir():
                raise self._fail(f"broken hard link: {src}") from exc

            path.parent.mkdir(parents=True, exist_ok=True)

        make(src, path)

    def apply_links(self) -> None:
        """Create the archive's links, once every file and directory is on disk.

        Walks the deferred members in stream order, so a hard link's target is
        always either a regular file the member loop already wrote or a symlink
        earlier in the list.

        Raises:
            ExtractionError: If a link would escape the destination through an
                earlier symlink.
        """
        symlinks: set[str] = set()

        for name, linkname, is_sym in self.deferred:
            folded = _fold(name)
            path = self.dest / name

            if is_sym:
                if _under_symlink(folded, symlinks):
                    raise self._fail(
                        f"unsafe tar member: symlink {name!r} sits under an "
                        f"earlier symlink"
                    )

                if self.sink is not None:
                    linkname = self.sink.link(name, str(path), linkname)

                self._make_link(os.symlink, linkname, path)
                symlinks.add(folded)
                continue

            if self.sink is not None:
                self.sink.hardlink(name, str(path), linkname)

            folded_target = _fold(linkname)
            if folded in symlinks or _under_symlink(folded, symlinks):
                raise self._fail(
                    f"unsafe tar member: hard link {name!r} sits at or under an "
                    f"earlier symlink"
                )

            if folded_target in symlinks or _under_symlink(folded_target, symlinks):
                raise self._fail(
                    f"unsafe tar member: hard link {name!r} aims at or through "
                    f"the symlink {linkname!r}"
                )

            self._make_link(os.link, str(self.dest / linkname), path)

    def apply_dir_modes(self) -> None:
        """Apply the archive's directory modes and mtimes, deepest name first."""
        for name, mode, mtime in sorted(self._dirs, key=lambda d: d[0], reverse=True):
            path = self._root + name
            os.utime(path, (mtime, mtime))
            if mode is not None:
                os.chmod(path, mode & _MODE_MASK)


def _extract_stream(fileobj: BinaryIO, extractor: _Extractor) -> None:
    """Extract a tar archive from a file-like object into a directory.

    Args:
        fileobj: The file-like object to read the archive from.
        extractor: The member loop to drive the archive through.
    """
    # 'r|*' auto-detects gzip vs uncompressed; the zstd caller has already
    # decompressed, so its stream arrives here as plain tar
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        extractor.extract(tar)


def extract_bottle(archive: Path, dest: Path, *, sink: _Sink | None = None) -> Path:
    """Extract `archive` into `dest` and return the keg directory.

    A bottle unpacks to `<name>/<version>/...` (plus a `<name>/.brew` metadata
    dir); the returned path the `<name>/<version>` keg root, which is what the
    relocator operates on.

    Args:
        archive: The path to the archive file.
        dest: The directory to extract the archive into.
        sink: A relocator to drive from the member loop, or None.

    Returns:
        The path to the extracted keg directory.
    """
    fmt = detect_format(archive)
    dest.mkdir(parents=True, exist_ok=True)
    extractor = _Extractor(dest, archive=archive, sink=sink)

    try:
        if fmt == "gzip":
            with archive.open("rb") as fh:
                _extract_stream(fh, extractor)

        else:  # zstd
            dctx = zstandard.ZstdDecompressor()
            with archive.open("rb") as fh, dctx.stream_reader(fh) as reader:
                _extract_stream(reader, extractor)

        # Links first, then modes: every directory is still writable here
        extractor.apply_links()
        extractor.apply_dir_modes()

    except tarfile.FilterError as exc:
        raise ExtractionError(
            f"unsafe tar member in {archive.name}: {exc}", archive=archive, dest=dest
        ) from exc

    # A placeholder the sink cannot resolve raises RelocationError
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
