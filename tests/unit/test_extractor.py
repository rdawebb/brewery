"""Unit tests for the bottle extractor."""

from __future__ import annotations

import tarfile

import pytest

from brewery.providers.extractor import (
    ExtractionError,
    _contained,
    _fold,
    _keg_filter,
    detect_format,
)

pytestmark = pytest.mark.unit


def _member(
    name: str,
    *,
    mode: int = 0o644,
    kind: bytes = tarfile.REGTYPE,
    linkname: str = "",
) -> tarfile.TarInfo:
    """Build a tar member carrying the ownership a real bottle records.

    Args:
        name: The member name.
        mode: The member's permission bits.
        kind: The member's tar type flag.
        linkname: The link target, for link members.

    Returns:
        The constructed tar member.
    """
    ti = tarfile.TarInfo(name)
    ti.mode = mode
    ti.type = kind
    ti.linkname = linkname
    ti.uid, ti.gid = 501, 20
    ti.uname, ti.gname = "brew", "staff"

    return ti


class TestDetectFormat:
    @pytest.mark.parametrize(
        "magic,expected",
        [
            (b"\x1f\x8b", "gzip"),
            (b"\x28\xb5\x2f\xfd", "zstd"),
        ],
        ids=["gzip", "zstd"],
    )
    def test_detect_format(self, tmp_path, magic, expected) -> None:
        """Test that magic bytes map to the correct format name."""
        arc = tmp_path / "archive"
        arc.write_bytes(magic + b"\x00" * 4)
        assert detect_format(arc) == expected

    def test_detect_format_rejects_unknown(self, tmp_path) -> None:
        """Test format detection rejects unknown formats."""
        arc = tmp_path / "weird"
        arc.write_bytes(b"PK\x03\x04rest-of-a-zip")
        with pytest.raises(ExtractionError, match="unrecognized"):
            detect_format(arc)


class TestContained:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("foo/1.0/bin/x", "foo/1.0/bin/x"),
            ("/foo/1.0/bin/x", "foo/1.0/bin/x"),  # Tar's leading '/' is stripped
            ("//foo//1.0/bin/x", "foo/1.0/bin/x"),
            ("./foo/1.0/bin/x", "foo/1.0/bin/x"),
            ("foo/1.0/bin/", "foo/1.0/bin"),
            ("foo..bar/1.0", "foo..bar/1.0"),  # '..' as a substring is not a
            ("", ""),  # Component, and names the destination root
            (".", ""),
            ("/", ""),
            ("../evil", None),
            ("foo/../../evil", None),
            ("foo/1.0/bin/../lib/x", None),  # Rejected, never collapsed
            ("foo/1.0/..", None),
            ("foo/1.0/bin/x\x00.dylib", None),
        ],
    )
    def test_contained(self, name, expected) -> None:
        """Test the lexical containment normaliser."""
        assert _contained(name) == expected


class TestFold:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Foo/Bar", "foo/bar"),
            ("caf\u00e9", "cafe\u0301"),  # NFC vs NFD "café"
        ],
        ids=["case", "unicode_normalisation"],
    )
    def test_equivalent_names_fold_together(self, left, right) -> None:
        """Test names the default macOS volume treats as one fold together."""
        assert _fold(left) == _fold(right)

    def test_distinct_names_stay_distinct(self) -> None:
        """Test folding does not collapse genuinely different names."""
        assert _fold("bin/openssl") != _fold("bin/openssl3")


class TestKegFilter:
    def test_ownership_is_dropped(self) -> None:
        """Test the filter blanks ownership so a root extraction cannot chown.

        `TarFile.chown` only acts when euid is 0, so this is not observable from
        an integration test that is not running as root.
        """
        out = _keg_filter()(_member("foo/1.0/bin/x"), "/stage")
        assert out is not None
        assert (out.uid, out.gid, out.uname, out.gname) == (None, None, None, None)

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [(0o4555, 0o555), (0o2755, 0o755), (0o1777, 0o777), (0o444, 0o444)],
        ids=["setuid", "setgid", "sticky", "readonly"],
    )
    def test_mode_keeps_permission_bits_only(self, mode, expected) -> None:
        """Test the mask drops setuid/setgid/sticky and keeps the rest."""
        out = _keg_filter()(_member("foo/1.0/bin/x", mode=mode), "/stage")
        assert out is not None
        assert out.mode == expected

    def test_filter_is_idempotent(self) -> None:
        """Test re-filtering a member is a no-op.

        `extractall` re-runs the filter on every directory member during its
        final attribute fixup pass, handing back the object the first call
        already mutated.
        """
        keg_filter = _keg_filter()
        member = _member("/foo/1.0/bin/", mode=0o4555, kind=tarfile.DIRTYPE)
        once = keg_filter(member, "/stage")
        assert once is not None
        twice = keg_filter(once, "/stage")
        assert twice is not None
        assert (
            (twice.name, twice.mode) == (once.name, once.mode) == ("foo/1.0/bin", 0o555)
        )

    def test_root_directory_member_is_skipped(self) -> None:
        """Test a member naming the destination root is dropped, not applied.

        An archive built as `tar -c .` opens with one; letting it through would
        put the archive's mode on the staging directory itself.
        """
        assert _keg_filter()(_member("./", kind=tarfile.DIRTYPE), "/stage") is None
