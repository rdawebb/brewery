"""Integration tests for the bottle extractor."""

import contextlib
import gzip
import io
import os
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
import zstandard

from brewery.providers import extractor
from brewery.providers.extractor import ExtractionError, extract_bottle

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

            elif kind == "fifo":
                _, name = entry
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.FIFOTYPE
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
    """Test extraction rejects path traversal in member names."""
    raw = make_tar([("file", "../evil", b"bad", 0o644)])
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


def test_absolute_symlink_allowed_for_bottles(tmp_path) -> None:
    """Test extraction allows absolute symlinks in sha-verified bottle archives."""
    raw = make_tar([("link", "foo/1.0/bin/x", "/usr/local/opt/foo/bin/x")])
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")
    assert os.readlink(keg / "bin" / "x") == "/usr/local/opt/foo/bin/x"


def test_escaping_relative_symlink_allowed_for_bottles(tmp_path) -> None:
    """Test escaping relative symlinks allowed in sha-verified bottle archives."""
    raw = make_tar([("link", "foo/1.0/share/foo", "../../../../share/foo")])
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")
    assert os.readlink(keg / "share" / "foo") == "../../../../share/foo"


def test_escaping_hardlink_rejected(tmp_path) -> None:
    """Test hardlinks escaping the destination are rejected."""
    raw = make_tar([("hardlink", "foo/1.0/bin/x", "../../../../../etc/passwd")])
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


def test_absolute_hardlink_rejected(tmp_path) -> None:
    """Test absolute hardlinks with a leading '/' are rejected."""
    raw = make_tar([("hardlink", "foo/1.0/bin/x", "/etc/passwd")])
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


def test_write_through_symlink_rejected(tmp_path) -> None:
    """Test that write-through symlinks are rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_bytes(b"original")
    raw = make_tar(
        [
            ("link", "foo/1.0/x", str(outside)),
            ("file", "foo/1.0/x/passwd", b"pwned", 0o644),
        ]
    )
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")

    assert (outside / "passwd").read_bytes() == b"original"


def test_file_replacing_symlink_rejected(tmp_path) -> None:
    """Test that file-replacing symlinks are rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_bytes(b"original")
    raw = make_tar(
        [
            ("link", "foo/1.0/x", str(outside / "passwd")),
            ("file", "foo/1.0/x", b"pwned", 0o644),
        ]
    )
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")

    assert (outside / "passwd").read_bytes() == b"original"


def test_write_through_symlink_not_followed_case_insensitively(tmp_path) -> None:
    """Test a differently-cased write-through symlink cannot escape.

    The default macOS volume is case-insensitive, so this tests that a
    write-through symlink with a differently-cased name cannot escape the archive's
    staging directory.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_bytes(b"original")
    raw = make_tar(
        [
            ("link", "foo/1.0/X", str(outside)),
            ("file", "foo/1.0/x/passwd", b"pwned", 0o644),
        ]
    )
    with contextlib.suppress(ExtractionError):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")

    assert (outside / "passwd").read_bytes() == b"original"


def test_hardlink_through_symlink_rejected(tmp_path) -> None:
    """Test that hardlinks through symlinks are rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"s3cret")
    raw = make_tar(
        [
            ("link", "foo/1.0/x", str(outside)),
            ("file", "foo/1.0/bin/real", b"ok", 0o644),
            ("hardlink", "foo/1.0/bin/leak", "foo/1.0/x/secret"),
        ]
    )
    stage = tmp_path / "stage"
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), stage)

    assert not (stage / "foo" / "1.0" / "bin" / "leak").exists()


def test_symlink_may_replace_earlier_symlink(tmp_path) -> None:
    """Test that symlinks over earlier symlinks are not rejected."""
    # makelink() unlinks the existing entry rather than following it
    raw = make_tar(
        [
            ("file", "foo/1.0/bin/a", b"a", 0o644),
            ("link", "foo/1.0/bin/x", "a"),
            ("link", "foo/1.0/bin/x", "b"),
        ]
    )
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")
    assert os.readlink(keg / "bin" / "x") == "b"


def test_hardlink_to_earlier_symlink_member_rejected(tmp_path) -> None:
    """Test a hard link aimed at a symlink the archive shipped is rejected."""
    raw = make_tar(
        [
            ("file", "foo/1.0/lib/libssl.dylib", b"MACHO", 0o444),
            ("link", "foo/1.0/lib/libssl.3.dylib", "libssl.dylib"),
            ("hardlink", "foo/1.0/lib/libssl.a", "foo/1.0/lib/libssl.3.dylib"),
        ]
    )
    stage = tmp_path / "stage"
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), stage)

    assert not (stage / "foo" / "1.0" / "lib" / "libssl.a").exists()


def test_symlink_lands_in_readonly_directory_member(tmp_path) -> None:
    """Test a link is created inside a directory the archive marks non-writable.

    `extractall` applies directory modes on its way out, so the parent is
    already `0o555` by the time the deferred link is created.
    """
    raw = make_tar(
        [
            ("dir", "foo/1.0/etc", 0o555),
            ("link", "foo/1.0/etc/openssl.cnf", "../share/openssl.cnf"),
        ]
    )
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")
    assert os.readlink(keg / "etc" / "openssl.cnf") == "../share/openssl.cnf"
    assert oct((keg / "etc").stat().st_mode & 0o777) == "0o555"
    (keg / "etc").chmod(0o755)  # So tmp_path can be torn down


def test_symlink_without_directory_members(tmp_path) -> None:
    """Test a symlink whose parent no member declares still gets one."""
    raw = make_tar([("link", "foo/1.0/bin/nested/x", "../../lib/x")])
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")
    assert os.readlink(keg / "bin" / "nested" / "x") == "../../lib/x"


def test_hardlink_within_keg_still_works(tmp_path) -> None:
    """Test that hardlinks within the keg (archive-root relative) still work."""
    raw = make_tar(
        [
            ("file", "foo/1.0/bin/qmake", b"MACHO", 0o555),
            ("hardlink", "foo/1.0/bin/qmake6", "foo/1.0/bin/qmake"),
        ]
    )
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")
    assert (keg / "bin" / "qmake6").read_bytes() == b"MACHO"
    assert (keg / "bin" / "qmake6").stat().st_ino == (
        keg / "bin" / "qmake"
    ).stat().st_ino


def test_dangling_hardlink_reports_extraction_error(tmp_path) -> None:
    """Test that dangling hardlinks report an extraction error."""
    raw = make_tar(
        [
            ("file", "foo/1.0/bin/qmake", b"MACHO", 0o555),
            ("hardlink", "foo/1.0/bin/qmake6", "foo/1.0/bin/absent"),
        ]
    )
    with pytest.raises(ExtractionError, match="broken hard link"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


def test_interior_traversal_rejected(tmp_path) -> None:
    """Test that interior traversal is rejected (defensive, no bottle contains one)."""
    raw = make_tar([("file", "foo/1.0/bin/../lib/x", b"ok", 0o644)])
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


def test_special_file_rejected(tmp_path) -> None:
    """Test that special files (fifo, device node) are rejected."""
    raw = make_tar([("fifo", "foo/1.0/bin/pipe")])
    with pytest.raises(ExtractionError, match="unsafe"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


def test_absolute_member_name_lands_inside_staging(tmp_path) -> None:
    """Test that a leading '/' member name lands inside the staging directory."""
    raw = make_tar([("file", "/foo/1.0/bin/x", b"ok", 0o644)])
    stage = tmp_path / "stage"
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), stage)
    assert keg == stage / "foo" / "1.0"
    assert (keg / "bin" / "x").read_bytes() == b"ok"


def test_dot_prefixed_member_names(tmp_path) -> None:
    """Test that './' prefixes and empty path segments are absorbed.

    The member naming the archive root normalises away entirely, so it is
    neither rejected as unsafe nor counted as a second top-level entry.
    """
    raw = make_tar(
        [
            ("dir", "./", 0o755),
            ("file", "./foo/1.0//bin/x", b"ok", 0o644),
        ]
    )
    stage = tmp_path / "stage"
    keg = extract_bottle(_archive(tmp_path, gzip.compress, raw), stage)
    assert keg == stage / "foo" / "1.0"
    assert (keg / "bin" / "x").read_bytes() == b"ok"


def test_locate_keg_rejects_symlink_top_level(tmp_path) -> None:
    """Test that a symlink top-level entry is rejected."""
    raw = make_tar([("link", "foo", str(tmp_path))])
    with pytest.raises(ExtractionError, match="single top-level keg"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


def test_extraction_stops_at_byte_ceiling(tmp_path, monkeypatch) -> None:
    """Test that a bottle expanding past the byte cap is rejected mid-stream.

    The cap is checked before the member is written, so the file that trips it
    never lands on disk.
    """
    monkeypatch.setattr(extractor, "_MAX_EXTRACTED_BYTES", 4096)
    raw = make_tar(
        [
            ("file", "foo/1.0/bin/small", b"x" * 1024, 0o644),
            ("file", "foo/1.0/bin/huge", b"x" * 8192, 0o644),
        ]
    )
    stage = tmp_path / "stage"
    with pytest.raises(ExtractionError, match="extraction limit"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), stage)

    assert not (stage / "foo" / "1.0" / "bin" / "huge").exists()


def test_extraction_stops_at_member_ceiling(tmp_path, monkeypatch) -> None:
    """Test that an archive with too many members is rejected.

    A byte cap alone misses the many-tiny-files shape, which exhausts inodes
    rather than space.
    """
    monkeypatch.setattr(extractor, "_MAX_MEMBERS", 3)
    raw = make_tar(
        [("file", f"foo/1.0/bin/f{i}", b"x", 0o644) for i in range(10)],
    )
    with pytest.raises(ExtractionError, match="more than 3 members"):
        extract_bottle(_archive(tmp_path, gzip.compress, raw), tmp_path / "stage")


def test_corrupt_archive_raises(tmp_path) -> None:
    """Test extraction raises for corrupt archives."""
    # Valid gzip magic, garbage payload -> decompression/tar error
    arc = tmp_path / "corrupt"
    arc.write_bytes(b"\x1f\x8b" + b"\x00" * 64)
    with pytest.raises(ExtractionError, match="failed to extract"):
        extract_bottle(arc, tmp_path / "stage")
