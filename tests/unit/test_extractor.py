"""Unit tests for the bottle extractor."""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import pytest

from brewery.providers.extractor import (
    ExtractionError,
    _apply_links,
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
        keg_filter, _ = _keg_filter()
        out = keg_filter(_member("foo/1.0/bin/x"), "/stage")
        assert out is not None
        assert (out.uid, out.gid, out.uname, out.gname) == (None, None, None, None)

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [(0o4555, 0o555), (0o2755, 0o755), (0o1777, 0o777), (0o444, 0o444)],
        ids=["setuid", "setgid", "sticky", "readonly"],
    )
    def test_mode_keeps_permission_bits_only(self, mode, expected) -> None:
        """Test the mask drops setuid/setgid/sticky and keeps the rest."""
        keg_filter, _ = _keg_filter()
        out = keg_filter(_member("foo/1.0/bin/x", mode=mode), "/stage")
        assert out is not None
        assert out.mode == expected

    def test_filter_is_idempotent(self) -> None:
        """Test re-filtering a member is a no-op.

        `extractall` re-runs the filter on every directory member during its
        final attribute fixup pass, handing back the object the first call
        already mutated.
        """
        keg_filter, _ = _keg_filter()
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
        keg_filter, _ = _keg_filter()
        assert keg_filter(_member("./", kind=tarfile.DIRTYPE), "/stage") is None

    @pytest.mark.parametrize(
        ("kind", "linkname", "expected"),
        [
            (
                tarfile.SYMTYPE,
                "../../../elsewhere",
                ("foo/1.0/bin/x", "../../../elsewhere", True),
            ),
            (
                tarfile.SYMTYPE,
                "/usr/local/opt/foo",
                ("foo/1.0/bin/x", "/usr/local/opt/foo", True),
            ),
            (
                tarfile.LNKTYPE,
                "./foo/1.0/bin/y",
                ("foo/1.0/bin/x", "foo/1.0/bin/y", False),
            ),
        ],
        ids=["escaping_symlink", "absolute_symlink", "hardlink"],
    )
    def test_link_members_are_deferred(self, kind, linkname, expected) -> None:
        """Test link members are held back for `_apply_links`, not extracted."""
        keg_filter, deferred = _keg_filter()
        out = keg_filter(
            _member("foo/1.0/bin/x", kind=kind, linkname=linkname), "/stage"
        )
        assert out is None
        assert deferred == [expected]

    def test_deferred_order_is_stream_order(self) -> None:
        """Test the deferred list keeps the order the archive declared."""
        keg_filter, deferred = _keg_filter()
        for name in ("foo/1.0/a", "foo/1.0/b", "foo/1.0/c"):
            keg_filter(_member(name, kind=tarfile.SYMTYPE, linkname="t"), "/stage")

        assert [name for name, _, _ in deferred] == [
            "foo/1.0/a",
            "foo/1.0/b",
            "foo/1.0/c",
        ]

    @pytest.mark.parametrize(
        ("linkname", "exc"),
        [
            ("/etc/passwd", tarfile.AbsoluteLinkError),
            ("../../../../etc/passwd", tarfile.LinkOutsideDestinationError),
        ],
        ids=["absolute", "escaping"],
    )
    def test_hardlink_targets_are_validated_before_deferral(
        self, linkname, exc
    ) -> None:
        """Test a hard link leaving the destination is rejected in the filter.

        Unlike a symlink, a hard link resolves at creation time, so its target
        has to be a member of this archive.
        """
        keg_filter, deferred = _keg_filter()
        with pytest.raises(exc):
            keg_filter(
                _member("foo/1.0/bin/x", kind=tarfile.LNKTYPE, linkname=linkname),
                "/stage",
            )

        assert deferred == []


class TestApplyLinks:
    def test_creates_links_in_order(self, tmp_path) -> None:
        """Test symlinks and hard links are created once files are on disk."""
        (tmp_path / "foo").mkdir()
        (tmp_path / "foo" / "real").write_bytes(b"MACHO")
        _apply_links(
            tmp_path,
            [("foo/sym", "real", True), ("foo/hard", "foo/real", False)],
        )
        assert os.readlink(tmp_path / "foo" / "sym") == "real"
        assert (tmp_path / "foo" / "hard").read_bytes() == b"MACHO"

    def test_missing_parent_is_created(self, tmp_path) -> None:
        """Test an archive shipping no directory members still links.

        The parent is only created on the `FileNotFoundError`, so the happy
        path never probes for it.
        """
        _apply_links(tmp_path, [("foo/1.0/bin/x", "../lib/x", True)])
        assert os.readlink(tmp_path / "foo" / "1.0" / "bin" / "x") == "../lib/x"

    def test_readonly_parent_is_reopened_and_restored(self, tmp_path) -> None:
        """Test a link lands in a directory `extractall` left non-writable."""
        parent = tmp_path / "etc"
        parent.mkdir(mode=0o555)
        _apply_links(tmp_path, [("etc/conf", "../share/conf", True)])
        assert os.readlink(parent / "conf") == "../share/conf"
        assert parent.stat().st_mode & 0o777 == 0o555
        parent.chmod(0o755)  # So tmp_path can be torn down

    def test_unowned_parent_reports_the_staging_directory(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test a parent this process cannot chmod names the real cause.

        Only the owner may chmod, so the refusal here is a staging directory
        belonging to another user, not an unsafe member.
        """
        parent = tmp_path / "etc"
        parent.mkdir(mode=0o555)

        def _refuse(self, mode: int) -> None:
            raise PermissionError(13, "Operation not permitted")

        monkeypatch.setattr(Path, "chmod", _refuse)

        with pytest.raises(ExtractionError, match="not owned by this user"):
            _apply_links(tmp_path, [("etc/conf", "../share/conf", True)])

        monkeypatch.undo()
        parent.chmod(0o755)  # So tmp_path can be torn down

    def test_symlink_replaces_earlier_symlink(self, tmp_path) -> None:
        """Test a duplicate link member replaces the earlier one."""
        _apply_links(tmp_path, [("x", "a", True), ("x", "b", True)])
        assert os.readlink(tmp_path / "x") == "b"

    def test_symlink_over_extracted_entry_rejected(self, tmp_path) -> None:
        """Test a symlink cannot replace a file or directory member.

        That shape is a file member written where a symlink was declared, which
        is the write-through escape seen from the other end.
        """
        (tmp_path / "x").write_bytes(b"already here")
        with pytest.raises(ExtractionError, match="unsafe"):
            _apply_links(tmp_path, [("x", "/etc", True)])

    def test_link_under_earlier_symlink_rejected(self, tmp_path) -> None:
        """Test a link cannot be created under a symlink the archive laid down."""
        with pytest.raises(ExtractionError, match="unsafe"):
            _apply_links(tmp_path, [("x", "/etc", True), ("x/passwd", "y", True)])

    @pytest.mark.parametrize(
        "target",
        ["x", "x/secret"],
        ids=["at_symlink", "through_symlink"],
    )
    def test_hardlink_at_or_through_symlink_rejected(self, tmp_path, target) -> None:
        """Test a hard link cannot resolve via a symlink the archive created.

        macOS `link()` follows a symlink source, so a hard link aimed at one
        would hand back the file it points to, outside the destination.
        """
        with pytest.raises(ExtractionError, match="unsafe"):
            _apply_links(tmp_path, [("x", "/etc", True), ("leak", target, False)])

    def test_hardlink_with_no_target_member_rejected(self, tmp_path) -> None:
        """Test a hard link naming a member the archive never wrote is reported."""
        with pytest.raises(ExtractionError, match="broken hard link"):
            _apply_links(tmp_path, [("x", "absent", False)])

    def test_symlink_set_folds_case_and_normalisation(self, tmp_path) -> None:
        """Test the tracked symlink names compare the way the volume does.

        Otherwise a symlink `X` followed by a hard link through `x` matches in
        the kernel and misses in Python.
        """
        with pytest.raises(ExtractionError, match="unsafe"):
            _apply_links(tmp_path, [("X", "/etc", True), ("leak", "x/passwd", False)])
