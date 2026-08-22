"""Unit tests for the bottle extractor."""

from __future__ import annotations

import os
import tarfile

import pytest

from brewery.providers.extractor import (
    ExtractionError,
    _contained,
    _Extractor,
    _fold,
    _Sink,
    detect_format,
)
from brewery.providers.relocator import StreamRelocator


def _apply_links(dest, deferred: list[tuple[str, str, bool]]) -> None:
    """Drive the link pass alone, over a staging directory built by hand.

    Args:
        dest: The staging directory the links are created in.
        deferred: Link members as `extract` would have held them back.
    """
    extractor = _Extractor(dest)
    extractor.deferred.extend(deferred)
    extractor.apply_links()


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


class TestAdmit:
    """Test the `_Extractor._admit` member screening loop."""

    def test_ownership_is_never_applied(self, tmp_path, monkeypatch) -> None:
        """Test the member loop never chowns, so a root extraction cannot."""
        for name in ("chown", "lchown", "fchown"):
            monkeypatch.setattr(
                os, name, lambda *a, **k: pytest.fail("extraction chowned a member")
            )

        extractor = _Extractor(tmp_path)
        extractor._directory(_member("foo", kind=tarfile.DIRTYPE), "foo")
        extractor.apply_dir_modes()

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [(0o4555, 0o555), (0o2755, 0o755), (0o1777, 0o777), (0o444, 0o444)],
        ids=["setuid", "setgid", "sticky", "readonly"],
    )
    def test_mode_keeps_permission_bits_only(self, tmp_path, mode, expected) -> None:
        """Test the mask drops setuid/setgid/sticky and keeps the rest."""
        extractor = _Extractor(tmp_path)
        extractor._directory(_member("bin", mode=mode, kind=tarfile.DIRTYPE), "bin")
        extractor.apply_dir_modes()

        assert (tmp_path / "bin").stat().st_mode & 0o7777 == expected
        (tmp_path / "bin").chmod(0o755)  # So tmp_path can be torn down

    def test_directory_modes_are_applied_deepest_first(self, tmp_path) -> None:
        """Test a read-only parent is locked only after its children are done."""
        extractor = _Extractor(tmp_path)
        for name, mode in (("etc", 0o555), ("etc/sub", 0o555)):
            extractor._directory(_member(name, mode=mode, kind=tarfile.DIRTYPE), name)

        extractor.apply_dir_modes()

        assert (tmp_path / "etc").stat().st_mode & 0o777 == 0o555
        assert (tmp_path / "etc" / "sub").stat().st_mode & 0o777 == 0o555
        (tmp_path / "etc").chmod(0o755)
        (tmp_path / "etc" / "sub").chmod(0o755)

    def test_root_directory_member_is_skipped(self, tmp_path) -> None:
        """Test a member naming the destination root is dropped, not applied.

        An archive built as `tar -c .` opens with one; letting it through would
        put the archive's mode on the staging directory itself.
        """
        extractor = _Extractor(tmp_path)
        assert extractor._admit(_member("./", kind=tarfile.DIRTYPE)) is None
        assert extractor._dirs == []

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
    def test_link_members_are_deferred(
        self, tmp_path, kind, linkname, expected
    ) -> None:
        """Test link members are held back for `_apply_links`, not written."""
        extractor = _Extractor(tmp_path)
        member = _member("foo/1.0/bin/x", kind=kind, linkname=linkname)

        assert extractor._admit(member) is None
        assert extractor.deferred == [expected]

    def test_deferred_order_is_stream_order(self, tmp_path) -> None:
        """Test the deferred list keeps the order the archive declared."""
        extractor = _Extractor(tmp_path)
        for name in ("foo/1.0/a", "foo/1.0/b", "foo/1.0/c"):
            extractor._admit(_member(name, kind=tarfile.SYMTYPE, linkname="t"))

        assert [name for name, _, _ in extractor.deferred] == [
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
        self, tmp_path, linkname, exc
    ) -> None:
        """Test a hard link leaving the destination is rejected up front.

        A hard link resolves at creation time, so its target has to be a member
        of this archive.
        """
        extractor = _Extractor(tmp_path)
        with pytest.raises(exc):
            extractor._admit(
                _member("foo/1.0/bin/x", kind=tarfile.LNKTYPE, linkname=linkname)
            )

        assert extractor.deferred == []


class TestApplyLinks:
    """Tests for _apply_links."""

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


class TestSinkProtocol:
    """Tests for the sink protocol."""

    def test_stream_relocator_satisfies_the_sink_protocol(self, tmp_path) -> None:
        """Test the relocator still implements the seam the member loop drives.

        The two modules are deliberately coupled only by duck typing. The
        annotation is what makes 'ty' check the pairing; the runtime assert
        catches a hook renamed on one side only.
        """
        sink: _Sink = StreamRelocator(
            prefix=tmp_path,
            cellar=tmp_path / "Cellar",
            repository=tmp_path / "Homebrew",
        )
        assert isinstance(sink.head_bytes, int)
