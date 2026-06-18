"""Unit tests for the keg linker."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import orjson
import pytest

import brewery.providers.linker as linker
from brewery.providers.linker import (
    _LINK_MANIFEST,
    LinkError,
    _points_into,
    link_keg,
    unlink_keg,
)


def _make_keg(prefix: Path, name: str = "openssl@3", version: str = "3.0") -> Path:
    """Create a synthetic Cellar keg with a representative layout.

    Args:
        prefix: The Homebrew prefix directory under which the keg is placed.
        name: The formula name (becomes the Cellar subdirectory name).
        version: The version string (becomes the keg version directory name).

    Returns:
        The path to the populated keg version directory.
    """
    keg = prefix / "Cellar" / name / version
    for d in [
        "bin",
        "lib/pkgconfig",
        "lib/engines-3",
        "include/openssl",
        "share/man/man1",
        "share/doc/openssl",
        ".brew",
    ]:
        (keg / d).mkdir(parents=True)
    (keg / "bin" / "openssl").write_text("#!/bin/sh\n")
    (keg / "lib" / "libssl.3.dylib").write_text("dylib")
    (keg / "lib" / "pkgconfig" / "openssl.pc").write_text("pc")
    (keg / "lib" / "engines-3" / "capi.dylib").write_text("engine")
    (keg / "include" / "openssl" / "ssl.h").write_text("h")
    (keg / "share" / "man" / "man1" / "openssl.1").write_text("man")
    (keg / "share" / "doc" / "openssl" / "README").write_text("doc")
    (keg / "INSTALL_RECEIPT.json").write_text("{}")
    (keg / ".brew" / f"{name}.rb").write_text("class")

    return keg


@pytest.fixture
def keg_and_prefix(tmp_path) -> tuple[Path, Path]:
    """A pre-built openssl@3 keg and its containing prefix directory.

    Args:
        tmp_path: The pytest-provided temporary directory.

    Returns:
        A tuple of (keg path, prefix path).
    """
    prefix = tmp_path / "prefix"
    keg = _make_keg(prefix)

    return keg, prefix


def _readlink(p: Path) -> str | None:
    """Return the symlink target of *p*, or None if *p* is not a symlink.

    Args:
        p: The path to inspect.

    Returns:
        The symlink target string, or None.
    """
    return os.readlink(p) if p.is_symlink() else None


def _mk(base: Path, rel: str, content: str = "x") -> Path:
    """Create a file at *base/rel*, making parent directories as needed.

    Args:
        base: The root directory under which the file is created.
        rel: The relative path of the file to create.
        content: The text content to write; defaults to ``'x'``.

    Returns:
        The absolute path to the created file.
    """
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)

    return p


def _points_to(link: Path, target: Path) -> bool:
    """Return True if *link* resolves to the same real path as *target*.

    Args:
        link: The symlink (or any path) to resolve.
        target: The expected real target path.

    Returns:
        True if both paths resolve to the same filesystem location.
    """
    return Path(os.path.realpath(link)) == target.resolve()


@pytest.fixture
def prefix(tmp_path: Path) -> Path:
    """A fresh, empty prefix directory.

    Args:
        tmp_path: The pytest-provided temporary directory.

    Returns:
        Path to the newly created prefix directory.
    """
    p = tmp_path / "prefix"
    p.mkdir()

    return p


class TestLinking:
    """Tests for the core link_keg strategy and output."""

    def test_bin_skips_absolute_target_symlinks(self, tmp_path) -> None:
        """Test that absolute-target symlinks in bin are not linked into the prefix."""
        prefix = tmp_path / "prefix"
        keg = prefix / "Cellar" / "node" / "26.0"
        (keg / "bin").mkdir(parents=True)
        (keg / "bin" / "node").write_text("real binary")  # Real file -> link
        os.symlink(
            "../lib/node_modules/corepack/dist/corepack.js", keg / "bin" / "corepack"
        )  # Relative in-keg -> link
        os.symlink(
            "/usr/local/lib/node_modules/npm/bin/npm-cli.js", keg / "bin" / "npm"
        )  # Absolute -> skip

        res = link_keg(keg, prefix=prefix, name="node")
        assert "bin/node" in res.linked
        assert "bin/corepack" in res.linked
        assert "bin/npm" not in res.linked
        assert not (prefix / "bin" / "npm").exists()

    def test_bin_files_are_relative_symlinks(self, keg_and_prefix) -> None:
        """Test that bin files are linked as relative symlinks pointing into the Cellar."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")
        link = prefix / "bin" / "openssl"
        assert link.is_symlink()
        assert _readlink(link) == "../Cellar/openssl@3/3.0/bin/openssl"
        assert link.resolve() == (keg / "bin" / "openssl").resolve()

    def test_lib_dylib_symlinked_whole(self, keg_and_prefix) -> None:
        """Test that top-level dylibs in lib are symlinked whole into the prefix."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")
        assert (
            _readlink(prefix / "lib" / "libssl.3.dylib")
            == "../Cellar/openssl@3/3.0/lib/libssl.3.dylib"
        )

    def test_lib_pkgconfig_is_mkpath_with_files_linked_inside(
        self, keg_and_prefix
    ) -> None:
        """Test that lib/pkgconfig is created as a real directory with files linked inside."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")
        pc_dir = prefix / "lib" / "pkgconfig"
        assert pc_dir.is_dir() and not pc_dir.is_symlink()  # Real shared dir
        assert (
            _readlink(pc_dir / "openssl.pc")
            == "../../Cellar/openssl@3/3.0/lib/pkgconfig/openssl.pc"
        )

    def test_non_mkpath_lib_subdir_linked_whole(self, keg_and_prefix) -> None:
        """Test that lib subdirs not in the mkpath set are linked whole, not descended."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")

        # engines-3 is not in the mkpath set -> linked whole, not descended.
        eng = prefix / "lib" / "engines-3"
        assert eng.is_symlink()
        assert _readlink(eng) == "../Cellar/openssl@3/3.0/lib/engines-3"

    def test_include_dir_linked_whole(self, keg_and_prefix) -> None:
        """Test that include subdirectories are linked whole into the prefix."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")
        inc = prefix / "include" / "openssl"
        assert inc.is_symlink()  # Not descended
        assert _readlink(inc) == "../Cellar/openssl@3/3.0/include/openssl"

    def test_share_man_is_mkpath(self, keg_and_prefix) -> None:
        """Test that share/man is expanded as a real directory tree with man pages linked."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")
        assert (prefix / "share" / "man").is_dir() and not (
            prefix / "share" / "man"
        ).is_symlink()

        # man tree is mkpath all the way down: manN is a real dir, pages link as files
        assert (prefix / "share" / "man" / "man1").is_dir()
        assert not (prefix / "share" / "man" / "man1").is_symlink()
        assert (prefix / "share" / "man" / "man1" / "openssl.1").is_symlink()

    def test_receipt_and_dotbrew_not_linked(self, keg_and_prefix) -> None:
        """Test that INSTALL_RECEIPT.json and .brew are excluded from linking."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")
        assert not (prefix / "INSTALL_RECEIPT.json").exists()
        assert not (prefix / ".brew").exists()

    def test_linked_record_created_and_relative(self, keg_and_prefix) -> None:
        """Test that the linked record symlink is created with a relative target."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")
        rec = prefix / "var" / "homebrew" / "linked" / "openssl@3"
        assert rec.is_symlink()
        assert _readlink(rec) == "../../../Cellar/openssl@3/3.0"
        assert rec.resolve() == keg.resolve()

    def test_link_result_contents(self, keg_and_prefix) -> None:
        """Test that the LinkResult lists the expected linked files and created directories."""
        keg, prefix = keg_and_prefix
        res = link_keg(keg, prefix=prefix, name="openssl@3")
        assert "bin/openssl" in res.linked
        assert "lib/pkgconfig/openssl.pc" in res.linked
        assert "lib/pkgconfig" in res.created_dirs
        assert "include/openssl" in res.linked  # Linked whole

    def test_relink_is_idempotent(self, keg_and_prefix) -> None:
        """Test that linking an already-linked keg reports already_linked without error."""
        keg, prefix = keg_and_prefix
        link_keg(keg, prefix=prefix, name="openssl@3")
        res2 = link_keg(keg, prefix=prefix, name="openssl@3")

        # Everything already points at this keg -> reported as already_linked, no error
        assert "bin/openssl" in res2.already_linked
        assert res2.linked == []  # Nothing new to create
        assert (prefix / "bin" / "openssl").is_symlink()

    def test_conflict_with_real_file_aborts_without_mutating(
        self, keg_and_prefix
    ) -> None:
        """Test that a pre-existing real file in the prefix aborts the link without any changes."""
        keg, prefix = keg_and_prefix
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "openssl").write_text("USER FILE")
        with pytest.raises(LinkError, match="openssl"):
            link_keg(keg, prefix=prefix, name="openssl@3")

        # Pre-pass aborted: the user's file is intact and nothing else was linked
        assert (prefix / "bin" / "openssl").read_text() == "USER FILE"
        assert not (prefix / "lib" / "libssl.3.dylib").exists()
        assert not (prefix / "var" / "homebrew" / "linked" / "openssl@3").exists()

    def test_conflict_with_other_keg_symlink_aborts(self, keg_and_prefix) -> None:
        """Test that a symlink to a different keg is treated as a conflict and aborts."""
        keg, prefix = keg_and_prefix
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "openssl").symlink_to("../Cellar/other/1.0/bin/openssl")
        with pytest.raises(LinkError):
            link_keg(keg, prefix=prefix, name="openssl@3")

    def test_overwrite_replaces_conflicting_file(self, keg_and_prefix) -> None:
        """Test that overwrite=True replaces a conflicting file in the prefix."""
        keg, prefix = keg_and_prefix
        (prefix / "bin").mkdir(parents=True)
        (prefix / "bin" / "openssl").write_text("USER FILE")
        res = link_keg(keg, prefix=prefix, name="openssl@3", overwrite=True)
        link = prefix / "bin" / "openssl"
        assert (
            link.is_symlink() and link.resolve() == (keg / "bin" / "openssl").resolve()
        )
        assert "bin/openssl" in res.linked

    def test_keg_only_is_a_noop(self, keg_and_prefix) -> None:
        """Test that keg_only=True skips all linking and creates no linked record."""
        keg, prefix = keg_and_prefix
        res = link_keg(keg, prefix=prefix, name="openssl@3", keg_only=True)
        assert res.linked == [] and res.created_dirs == []
        assert not (prefix / "bin" / "openssl").exists()
        assert not (prefix / "var" / "homebrew" / "linked" / "openssl@3").exists()

    def test_etc_existing_config_preserved(self, tmp_path) -> None:
        """Test that a pre-existing file under etc is treated as already-linked, not a conflict."""
        prefix = tmp_path / "prefix"
        keg = _make_keg(prefix)
        (keg / "etc").mkdir()
        (keg / "etc" / "foo.conf").write_text("default")
        (prefix / "etc").mkdir()
        (prefix / "etc" / "foo.conf").write_text("USER EDITED")
        res = link_keg(keg, prefix=prefix, name="openssl@3")
        assert (prefix / "etc" / "foo.conf").read_text() == "USER EDITED"  # Preserved
        assert (
            "etc/foo.conf" in res.already_linked
        )  # Rreated as already-satisfied, not a conflict

    def test_etc_new_file_is_linked(self, tmp_path) -> None:
        """Test that a new file in keg etc with no existing counterpart is linked."""
        prefix = tmp_path / "prefix"
        keg = _make_keg(prefix)
        (keg / "etc").mkdir()
        (keg / "etc" / "new.conf").write_text("default")
        link_keg(keg, prefix=prefix, name="openssl@3")
        assert (prefix / "etc" / "new.conf").is_symlink()

    def test_missing_eligible_dir_is_skipped(self, tmp_path) -> None:
        """Test that eligible top-level dirs absent from the keg are silently skipped."""
        prefix = tmp_path / "prefix"
        keg = prefix / "Cellar" / "tiny" / "1.0"
        (keg / "lib").mkdir(parents=True)
        (keg / "lib" / "libtiny.dylib").write_text("x")  # No bin/include/share
        res = link_keg(keg, prefix=prefix, name="tiny")
        assert res.linked == ["lib/libtiny.dylib"]


class TestLinkExplosion:
    """Tests for symlink-explosion handling"""

    def test_second_keg_explodes_whole_dir_symlink(self, tmp_path, prefix) -> None:
        """Test that linking a second keg into a whole-dir symlink explodes it into a real directory."""
        cellar = tmp_path / "Cellar"
        a = cellar / "xorgproto/2025.1"
        _mk(a, "include/X11/Xfuncproto.h")
        b = cellar / "libx11/1.8.13"
        _mk(b, "include/X11/Xlib.h")

        link_keg(a, prefix=prefix, name="xorgproto")
        x11 = prefix / "include/X11"
        assert x11.is_symlink()  # First keg: whole-dir symlink

        res = link_keg(b, prefix=prefix, name="libx11")

        assert x11.is_dir() and not x11.is_symlink()  # Exploded into a real dir
        assert _points_to(
            x11 / "Xfuncproto.h", a / "include/X11/Xfuncproto.h"
        )  # A relinked
        assert _points_to(x11 / "Xlib.h", b / "include/X11/Xlib.h")  # B linked
        assert "include/X11/Xlib.h" in res.linked

    def test_third_keg_descends_real_dir(self, tmp_path, prefix) -> None:
        """Test that a third keg correctly descends an already-exploded real directory."""
        cellar = tmp_path / "Cellar"
        a = cellar / "xorgproto/2025.1"
        _mk(a, "include/X11/Xfuncproto.h")
        b = cellar / "libx11/1.8.13"
        _mk(b, "include/X11/Xlib.h")
        c = cellar / "libxau/1.0.12"
        _mk(c, "include/X11/Xauth.h")

        link_keg(a, prefix=prefix, name="xorgproto")
        link_keg(b, prefix=prefix, name="libx11")  # Explodes
        link_keg(c, prefix=prefix, name="libxau")  # Descends the real dir

        x11 = prefix / "include/X11"
        assert _points_to(x11 / "Xfuncproto.h", a / "include/X11/Xfuncproto.h")
        assert _points_to(x11 / "Xlib.h", b / "include/X11/Xlib.h")
        assert _points_to(x11 / "Xauth.h", c / "include/X11/Xauth.h")

    def test_shared_subdir_recurses(self, tmp_path, prefix) -> None:
        """Test that a shared subdirectory within an exploded dir is itself realised and merged."""
        cellar = tmp_path / "Cellar"
        a = cellar / "xorgproto/2025.1"
        _mk(a, "include/X11/extensions/Xext.h")
        b = cellar / "libx11/1.8.13"
        _mk(b, "include/X11/extensions/shape.h")

        link_keg(a, prefix=prefix, name="xorgproto")
        link_keg(b, prefix=prefix, name="libx11")

        ext = prefix / "include/X11/extensions"
        assert ext.is_dir() and not ext.is_symlink()  # Shared subdir realised too
        assert _points_to(ext / "Xext.h", a / "include/X11/extensions/Xext.h")
        assert _points_to(ext / "shape.h", b / "include/X11/extensions/shape.h")

    def test_file_collision_aborts_without_mutating(self, tmp_path, prefix) -> None:
        """Test that a same-named file across two kegs aborts explosion without mutating the prefix."""
        cellar = tmp_path / "Cellar"
        a = cellar / "xorgproto/2025.1"
        _mk(a, "include/X11/Xfuncproto.h")
        b = cellar / "libx11/1.8.13"
        _mk(b, "include/X11/Xfuncproto.h")  # Same file -> genuine conflict
        _mk(b, "include/X11/Xlib.h")

        link_keg(a, prefix=prefix, name="xorgproto")
        x11 = prefix / "include/X11"

        with pytest.raises(LinkError):
            link_keg(b, prefix=prefix, name="libx11")

        # Nothing mutated: still A's whole-dir symlink, B's files not linked
        assert x11.is_symlink()
        assert _points_to(x11, a / "include/X11")

    def test_explosion_is_idempotent(self, tmp_path, prefix) -> None:
        """Test that re-linking an already-exploded keg reports already_linked without re-exploding."""
        cellar = tmp_path / "Cellar"
        a = cellar / "xorgproto/2025.1"
        _mk(a, "include/X11/Xfuncproto.h")
        b = cellar / "libx11/1.8.13"
        _mk(b, "include/X11/Xlib.h")

        link_keg(a, prefix=prefix, name="xorgproto")
        link_keg(b, prefix=prefix, name="libx11")
        res2 = link_keg(b, prefix=prefix, name="libx11")  # Re-link: no second explosion

        x11 = prefix / "include/X11"
        assert x11.is_dir() and not x11.is_symlink()
        assert "include/X11/Xlib.h" in res2.already_linked
        assert _points_to(x11 / "Xlib.h", b / "include/X11/Xlib.h")

    def test_metapackage_symlink_to_whole_dir_link_is_skipped(
        self, tmp_path, prefix
    ) -> None:
        """Test that an umbrella keg's symlink pointing at an existing whole-dir prefix link
        is treated as already-linked."""
        cellar = tmp_path / "Cellar"
        qtbase = cellar / "qtbase/6.11.1"
        _mk(qtbase, "lib/cmake/Qt6Gui/Qt6GuiConfig.cmake")
        link_keg(qtbase, prefix=prefix, name="qtbase")
        gui = prefix / "lib/cmake/Qt6Gui"
        assert gui.is_symlink()  # Only qtbase provides it -> whole-dir symlink

        # Umbrella ships lib/cmake/Qt6Gui as a symlink pointing at the prefix
        # location, so it resolves to the same dir
        qt = cellar / "qt/6.11.1"
        (qt / "lib/cmake").mkdir(parents=True)
        os.symlink(os.path.relpath(gui, qt / "lib/cmake"), qt / "lib/cmake/Qt6Gui")

        res = link_keg(qt, prefix=prefix, name="qt")  # Should not conflict
        assert "lib/cmake/Qt6Gui" in res.already_linked
        assert _points_to(gui, qtbase / "lib/cmake/Qt6Gui")  # Unchanged

    def test_metapackage_symlink_over_exploded_dir_is_skipped(
        self, tmp_path, prefix
    ) -> None:
        """Test that an umbrella keg's symlink pointing at an already-exploded real directory
        is treated as already-linked."""
        cellar = tmp_path / "Cellar"
        qtbase = cellar / "qtbase/6.11.1"
        _mk(qtbase, "lib/cmake/Qt6BuildInternals/a.cmake")
        qttools = cellar / "qttools/6.11.1"
        _mk(qttools, "lib/cmake/Qt6BuildInternals/b.cmake")
        link_keg(qtbase, prefix=prefix, name="qtbase")
        link_keg(qttools, prefix=prefix, name="qttools")  # Explodes Qt6BuildInternals
        bi = prefix / "lib/cmake/Qt6BuildInternals"
        assert bi.is_dir() and not bi.is_symlink()  # Exploded real dir

        # Umbrella's entry is a symlink at the prefix location & it resolves to the
        # exploded real dir, so the whole entry is already satisfied
        qt = cellar / "qt/6.11.1"
        (qt / "lib/cmake").mkdir(parents=True)
        os.symlink(
            os.path.relpath(bi, qt / "lib/cmake"), qt / "lib/cmake/Qt6BuildInternals"
        )

        res = link_keg(qt, prefix=prefix, name="qt")  # Should not conflict
        assert "lib/cmake/Qt6BuildInternals" in res.already_linked
        assert _points_to(
            bi / "a.cmake", qtbase / "lib/cmake/Qt6BuildInternals/a.cmake"
        )
        assert _points_to(
            bi / "b.cmake", qttools / "lib/cmake/Qt6BuildInternals/b.cmake"
        )

    def test_merge_collisions_ignores_same_realpath(self, tmp_path) -> None:
        """Test that two entries resolving to the same real file are not reported as a collision."""
        a = tmp_path / "a"
        _mk(a, "objs/qrc.o", "data")
        b = tmp_path / "b"
        (b / "objs").mkdir(parents=True)
        os.symlink(
            os.path.relpath(a / "objs/qrc.o", b / "objs"), b / "objs/qrc.o"
        )  # b -> a's file

        # Both "provide" objs/qrc.o, but should resolve to one file -> not a conflict
        assert (
            linker._merge_collisions(Path("/prefix/objs"), a / "objs", b / "objs") == []
        )


class TestUnlink:
    """Tests for unlink_keg's manifest fast-path, realpath filter, and fallback."""

    def test_manifest_written_on_link(self, tmp_path, prefix) -> None:
        """link_keg records the candidate set in the keg."""
        cellar = tmp_path / "Cellar"
        keg = cellar / "tool/1.0"
        _mk(keg, "bin/tool", "x")
        _mk(keg, "lib/libfoo.dylib", "y")
        link_keg(keg, prefix=prefix, name="tool")
        data = orjson.loads((keg / _LINK_MANIFEST).read_text())
        assert set(data["linked"]) == {"bin/tool", "lib/libfoo.dylib"}

    def test_unlink_removes_recorded_links(self, tmp_path, prefix) -> None:
        """The fast path removes every recorded link without scanning."""
        cellar = tmp_path / "Cellar"
        keg = cellar / "tool/1.0"
        _mk(keg, "bin/tool", "x")
        _mk(keg, "lib/libfoo.dylib", "y")
        link_keg(keg, prefix=prefix, name="tool")
        res = unlink_keg(keg, prefix=prefix, name="tool")
        assert res.scanned is False
        assert set(res.removed) == {"bin/tool", "lib/libfoo.dylib"}
        assert not (prefix / "bin" / "tool").exists()
        assert not (prefix / "lib" / "libfoo.dylib").exists()

    def test_unlink_skips_foreign_owned_path(self, tmp_path, prefix) -> None:
        """A recorded link now resolving into another keg is left alone."""
        cellar = tmp_path / "Cellar"
        a = cellar / "a/1.0"
        _mk(a, "bin/shared", "a")
        b = cellar / "b/1.0"
        _mk(b, "bin/shared", "b")
        link_keg(a, prefix=prefix, name="a")
        link_keg(b, prefix=prefix, name="b", overwrite=True)  # b takes the path
        res = unlink_keg(a, prefix=prefix, name="a")
        assert "bin/shared" not in res.removed
        link = prefix / "bin" / "shared"
        assert link.is_symlink()
        assert os.path.realpath(link) == os.path.realpath(b / "bin" / "shared")

    def test_unlink_explosion_stragglers(self, tmp_path, prefix) -> None:
        """When a whole-dir link was exploded by a later keg, only our files go."""
        cellar = tmp_path / "Cellar"
        a = cellar / "a/1.0"
        _mk(a, "lib/shared/a.txt", "a")
        b = cellar / "b/1.0"
        _mk(b, "lib/shared/b.txt", "b")
        link_keg(a, prefix=prefix, name="a")
        assert (prefix / "lib" / "shared").is_symlink()  # Whole-dir link
        link_keg(b, prefix=prefix, name="b")
        shared = prefix / "lib" / "shared"
        assert shared.is_dir() and not shared.is_symlink()  # Exploded
        res = unlink_keg(a, prefix=prefix, name="a")
        assert "lib/shared/a.txt" in res.removed
        assert not (shared / "a.txt").exists()
        assert (shared / "b.txt").is_symlink()  # b's straggler survives
        assert shared.is_dir()

    def test_unlink_no_manifest_falls_back_to_scan(self, tmp_path, prefix) -> None:
        """A keg with no manifest is unlinked by scanning the eligible roots."""
        cellar = tmp_path / "Cellar"
        keg = cellar / "tool/1.0"
        _mk(keg, "bin/tool", "x")
        link_keg(keg, prefix=prefix, name="tool")
        (keg / _LINK_MANIFEST).unlink()  # Simulate a brew-installed keg
        res = unlink_keg(keg, prefix=prefix, name="tool")
        assert res.scanned is True
        assert "bin/tool" in res.removed
        assert not (prefix / "bin" / "tool").exists()

    def test_unlink_prunes_emptied_dirs(self, tmp_path, prefix) -> None:
        """Emptied mkpath dirs are pruned; the eligible root is kept."""
        cellar = tmp_path / "Cellar"
        keg = cellar / "tool/1.0"
        _mk(keg, "share/man/man1/tool.1", "x")
        link_keg(keg, prefix=prefix, name="tool")
        res = unlink_keg(keg, prefix=prefix, name="tool")
        assert (prefix / "share").is_dir()  # Eligible root kept
        assert not (prefix / "share" / "man").exists()  # Emptied dirs pruned
        assert "share/man/man1" in res.pruned

    def test_unlink_clears_linked_record_when_ours(self, tmp_path, prefix) -> None:
        """The linked-keg pointer is removed when it points at this keg."""
        cellar = tmp_path / "Cellar"
        keg = cellar / "tool/1.0"
        _mk(keg, "bin/tool", "x")
        link_keg(keg, prefix=prefix, name="tool")
        record = prefix / "var" / "homebrew" / "linked" / "tool"
        assert record.is_symlink()
        unlink_keg(keg, prefix=prefix, name="tool")
        assert not record.is_symlink()

    def test_unlink_keeps_foreign_linked_record(self, tmp_path, prefix) -> None:
        """The pointer is left alone when it points at a different keg."""
        cellar = tmp_path / "Cellar"
        v1 = cellar / "a/1.0"
        _mk(v1, "bin/a", "x")
        v2 = cellar / "a/2.0"
        _mk(v2, "bin/a", "x")
        link_keg(v1, prefix=prefix, name="a")
        record = prefix / "var" / "homebrew" / "linked" / "a"
        record.unlink()
        record.symlink_to(os.path.relpath(v2, record.parent))  # Repoint to 2.0
        unlink_keg(v1, prefix=prefix, name="a")
        assert record.is_symlink()

    def test_unlink_keg_only_is_noop(self, tmp_path, prefix) -> None:
        """A keg-only keg has nothing linked and unlinks to an empty result."""
        cellar = tmp_path / "Cellar"
        keg = cellar / "tool/1.0"
        _mk(keg, "lib/libtool.dylib", "x")
        link_keg(
            keg, prefix=prefix, name="tool", keg_only=True
        )  # No links, no manifest
        res = unlink_keg(keg, prefix=prefix, name="tool")
        assert res.removed == []
        assert res.scanned is True  # No manifest -> scan finds nothing

    def test_points_into(self, tmp_path) -> None:
        """_points_into is True for a link into the keg, False otherwise."""
        keg = tmp_path / "keg"
        (keg / "bin").mkdir(parents=True)
        (keg / "bin" / "x").write_text("x")
        (tmp_path / "other").write_text("o")
        inside = tmp_path / "inside"
        inside.symlink_to(keg / "bin" / "x")
        outside = tmp_path / "outside"
        outside.symlink_to(tmp_path / "other")
        assert _points_into(inside, keg.resolve()) is True
        assert _points_into(outside, keg.resolve()) is False

    def test_unlink_removes_opt_link(self, tmp_path, prefix) -> None:
        """The opt link is removed so no broken symlink survives the keg."""
        cellar = tmp_path / "Cellar"
        keg = cellar / "openssl@3" / "3.0"
        _mk(keg, "bin/openssl", "x")
        (prefix / "opt").mkdir(parents=True, exist_ok=True)
        (prefix / "opt" / "openssl@3").symlink_to(keg)
        link_keg(keg, prefix=prefix, name="openssl@3")
        unlink_keg(keg, prefix=prefix, name="openssl@3")
        assert not (prefix / "opt" / "openssl@3").exists()

    def test_unlink_keeps_opt_link_for_other_version(self, tmp_path, prefix) -> None:
        """Unlinking a stale keg leaves opt pointing at the active one."""
        cellar = tmp_path / "Cellar"
        old = cellar / "openssl@3" / "3.0"
        new = cellar / "openssl@3" / "3.1"
        _mk(old, "bin/openssl", "x")
        _mk(new, "bin/openssl", "x")
        (prefix / "opt").mkdir(parents=True, exist_ok=True)
        (prefix / "opt" / "openssl@3").symlink_to(new)  # opt -> active (3.1)
        unlink_keg(old, prefix=prefix, name="openssl@3")
        assert (prefix / "opt" / "openssl@3").is_symlink()  # Untouched


_CANDIDATES = ["gettext", "python@3.13", "python@3.14", "node", "openssl@3"]


@pytest.mark.integration
@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("brew") is None,
    reason="requires macOS with Homebrew",
)
def test_plan_matches_brew_links() -> None:
    """Non-destructive check: planned links must match those brew actually created."""
    prefix = Path(
        subprocess.run(
            ["brew", "--prefix"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )

    formula = keg = None
    for cand in _CANDIDATES:
        cellar = prefix / "Cellar" / cand
        if not cellar.is_dir():
            continue
        versions = [p for p in cellar.iterdir() if p.is_dir()]

        # Only useful if it's actually linked (not keg-only / unlinked)
        record = prefix / "var" / "homebrew" / "linked" / cand
        if versions and record.is_symlink():
            formula, keg = cand, versions[-1]
            break
    if keg is None:
        pytest.skip("none of the candidate formulae are installed and linked")

    plan = linker._build_plan(keg, prefix)

    # Against an already-linked keg, every target lands in `already`, not `links`
    brewery_links = {str(dst) for dst, _ in plan.links} | {str(p) for p in plan.already}

    # brew's real links into this keg, restricted to the eligible roots.
    keg_real = os.path.realpath(keg)
    brew_links: set[str] = set()
    for sub in linker._ELIGIBLE:
        root = prefix / sub
        if root.is_dir() and not root.is_symlink():
            brew_links |= _symlinks_into(root, keg_real)

    missing = brew_links - brewery_links  # Sstrategy gap
    spurious = brewery_links - brew_links  # Over-linking
    assert not spurious, (
        f"{formula}: would create links, brew did not: {sorted(spurious)}"
    )
    assert not missing, f"{formula}: strategy gap, brew links missed: {sorted(missing)}"


def _symlinks_into(root: Path, keg_real: str) -> set[str]:
    """Collect symlinks under *root* that resolve into *keg_real*, descending only real directories.

    Args:
        root: The prefix subdirectory to scan (e.g. ``prefix / 'bin'``).
        keg_real: The real (resolved) path of the keg as a string; only symlinks
            whose real target starts with this prefix are collected.

    Returns:
        The set of absolute path strings for every matching symlink found.
    """
    found: set[str] = set()
    stack = [str(root)]
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in it:
                if e.is_symlink():
                    if os.path.realpath(e.path).startswith(keg_real):
                        found.add(e.path)

                elif e.is_dir(follow_symlinks=False):
                    stack.append(e.path)

    return found
