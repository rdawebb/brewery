"""Integration tests for the bottle extractor."""

import gzip
import io
import os
import tarfile
from pathlib import Path
from typing import Callable

import pytest
import zstandard

from brewery.providers.extractor import ExtractionError, extract_bottle

pytestmark = pytest.mark.integration

COMPRESSORS = {
    "gzip": gzip.compress,
    "zstd": lambda b: zstandard.ZstdCompressor().compress(b),
}


def make_tar(entries: list[tuple]) -> bytes:
    """Create a tar archive from the given entries.

    entries: ('file', name, data, mode) | ('dir', name, mode) | ('link', name, target).

    Args:
        entries: The list of entries to include in the tar archive.

    Returns:
        A bytes object containing the tar archive.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for entry in entries:
            kind = entry[0]
            if kind == "file":
                _, name, data, mode = entry
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                ti.mode = mode
                t.addfile(ti, io.BytesIO(data))

            elif kind == "dir":
                _, name, mode = entry
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE
                ti.mode = mode
                t.addfile(ti)

            elif kind == "link":
                _, name, target = entry
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = target
                t.addfile(ti)

            elif kind == "hardlink":
                _, name, target = entry
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.LNKTYPE
                ti.linkname = target
                t.addfile(ti)

            else:
                raise ValueError(kind)

    return buf.getvalue()


def standard_keg(name: str = "openssl@3", version: str = "3.0") -> list[tuple]:
    """Create a standard keg structure for the given formula.

    Args:
        name: The name of the formula.
        version: The version of the formula.

    Returns:
        A list of tuples representing the keg structure.
    """
    base = f"{name}/{version}"
    return [
        ("file", f"{base}/bin/openssl", b"MACHO-binary", 0o555),
        ("file", f"{base}/lib/libssl.dylib", b"@@HOMEBREW_PREFIX@@/lib", 0o444),
        ("link", f"{base}/lib/libssl.3.dylib", "libssl.dylib"),  # Relative
        # Placeholder target, relative as far as the filter is concerned
        (
            "link",
            f"{base}/bin/openssl-link",
            "@@HOMEBREW_PREFIX@@/opt/openssl@3/bin/openssl",
        ),
        ("dir", f"{name}/.brew", 0o755),
        ("file", f"{name}/.brew/{name}.rb", b"class Openssl3\nend\n", 0o644),
    ]


@pytest.fixture(params=list(COMPRESSORS))
def fmt(request) -> str:
    """Fixture for compression format.

    Args:
        request: The pytest request object.

    Returns:
        The compression format to use.
    """
    return request.param


@pytest.fixture
def compress(fmt) -> Callable[[bytes], bytes]:
    """Fixture for compression function.

    Args:
        fmt: The compression format to use.

    Returns:
        The compression function to use.
    """
    return COMPRESSORS[fmt]


def _archive(tmp_path: Path, compress, raw: bytes, name: str = "bottle") -> Path:
    """Helper to write a compressed archive to a temp file.

    Args:
        tmp_path: The temporary path to write the archive to.
        compress: The compression function to use.
        raw: The raw bytes to compress.
        name: The name of the archive file.

    Returns:
        The path to the created archive file.
    """
    p = tmp_path / name
    p.write_bytes(compress(raw))

    return p


def test_extract_returns_keg_root_ignoring_dotbrew(tmp_path, compress) -> None:
    """Test extraction returns keg root ignoring .brew directory."""
    arc = _archive(tmp_path, compress, make_tar(standard_keg()))
    dest = tmp_path / "stage"
    keg = extract_bottle(arc, dest)
    assert keg == dest / "openssl@3" / "3.0"
    assert (keg / "bin" / "openssl").read_bytes() == b"MACHO-binary"


def test_extract_preserves_readonly_modes(tmp_path, compress) -> None:
    """Test extraction preserves readonly modes."""
    arc = _archive(tmp_path, compress, make_tar(standard_keg()))
    keg = extract_bottle(arc, tmp_path / "stage")
    assert oct((keg / "bin" / "openssl").stat().st_mode & 0o777) == "0o555"
    assert oct((keg / "lib" / "libssl.dylib").stat().st_mode & 0o777) == "0o444"


def test_extract_preserves_relative_symlink(tmp_path, compress) -> None:
    """Test extraction preserves relative symlink."""
    arc = _archive(tmp_path, compress, make_tar(standard_keg()))
    keg = extract_bottle(arc, tmp_path / "stage")
    assert os.readlink(keg / "lib" / "libssl.3.dylib") == "libssl.dylib"


def test_extract_preserves_placeholder_symlink_target(tmp_path, compress) -> None:
    """Test extraction preserves placeholder symlink target."""
    arc = _archive(tmp_path, compress, make_tar(standard_keg()))
    keg = extract_bottle(arc, tmp_path / "stage")
    assert (
        os.readlink(keg / "bin" / "openssl-link")
        == "@@HOMEBREW_PREFIX@@/opt/openssl@3/bin/openssl"
    )


def test_extract_drops_setuid_bit(tmp_path, compress) -> None:
    """Test extraction drops setuid bit."""
    raw = make_tar([("file", "foo/1.0/bin/suid", b"x", 0o4555)])
    keg = extract_bottle(_archive(tmp_path, compress, raw), tmp_path / "stage")
    mode = (keg / "bin" / "suid").stat().st_mode
    assert not mode & 0o4000  # setuid stripped
    assert oct(mode & 0o777) == "0o555"  # Permission bits intact


def test_path_traversal_rejected(tmp_path) -> None:
    # A file member whose name escapes the destination is still rejected.
    raw = make_tar([("file", "../evil", b"bad", 0o644)])
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


def test_absolute_symlink_allowed_for_bottles(tmp_path) -> None:
    # brew creates absolute symlinks in kegs (some get relocated); for a
    # sha-verified bottle we create them as-is rather than rejecting.
    raw = make_tar([("link", "foo/1.0/bin/x", "/usr/local/opt/foo/bin/x")])
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")
    assert os.readlink(keg / "bin" / "x") == "/usr/local/opt/foo/bin/x"


def test_escaping_relative_symlink_allowed_for_bottles(tmp_path) -> None:
    # hunspell ships a symlink whose relative target escapes the keg root; the
    # stdlib data filter rejects it, but brew creates it, so we must too.
    raw = make_tar([("link", "foo/1.0/share/foo", "../../../../share/foo")])
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")
    assert os.readlink(keg / "share" / "foo") == "../../../../share/foo"


def test_escaping_hardlink_rejected(tmp_path) -> None:
    # Hardlinks that escape the destination remain rejected -- the relaxation is
    # symlink-only (a hardlink to outside the tree is a genuine hazard).
    raw = make_tar([("hardlink", "foo/1.0/bin/x", "../../../../../etc/passwd")])
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


@pytest.mark.parametrize(
    ("entries", "match"),
    [
        pytest.param(
            [("file", "../evil", b"bad", 0o644)], "unsafe", id="path_traversal"
        ),
        pytest.param(
            [
                ("file", "foo/1.0/bin/a", b"a", 0o644),
                ("file", "bar/1.0/bin/b", b"b", 0o644),
            ],
            "single top-level keg",
            id="multiple_top_dirs",
        ),
        pytest.param(
            [
                ("file", "foo/1.0/bin/a", b"a", 0o644),
                ("file", "foo/2.0/bin/b", b"b", 0o644),
            ],
            "one version dir",
            id="multiple_version_dirs",
        ),
        # Only a .brew dir under the name -> no version dir to return
        pytest.param(
            [("file", "foo/.brew/foo.rb", b"x", 0o644)],
            "one version dir",
            id="no_version_dir",
        ),
        # brew creates absolute symlinks in kegs (some get relocated)
        pytest.param(
            [("link", "foo/1.0/bin/x", "/usr/local/opt/foo/bin/x")],
            None,
            id="absolute_symlink_allowed",
        ),
        pytest.param(
            [("link", "foo/1.0/share/foo", "../../../../share/foo")],
            None,
            id="escaping_relative_symlink_allowed",
        ),
        pytest.param(
            [("hardlink", "foo/1.0/bin/x", "../../../../../etc/passwd")],
            "unsafe",
            id="escaping_hardlink",
        ),
    ],
)
def test_extract_rejects_unsafe_or_malformed(tmp_path, entries, match) -> None:
    """Test that unsafe paths and malformed keg layouts are rejected.

    These safety/layout cases use gzip for brevity (format independence is
    covered by the happy-path tests parametrized over fmt). match=None means
    the case is expected to succeed.
    """
    arc = _archive(tmp_path, gzip.compress, make_tar(entries))
    if match is None:
        extract_bottle(arc, tmp_path / "stage")

    else:
        with pytest.raises(ExtractionError, match=match):
            extract_bottle(arc, tmp_path / "stage")


def test_corrupt_archive_raises(tmp_path) -> None:
    """Test extraction raises for corrupt archives."""
    # Valid gzip magic, garbage payload -> decompression/tar error
    arc = tmp_path / "corrupt"
    arc.write_bytes(b"\x1f\x8b" + b"\x00" * 64)
    with pytest.raises(ExtractionError, match="failed to extract"):
        extract_bottle(arc, tmp_path / "stage")
