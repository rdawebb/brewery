"""Unit tests for the write-bit hold and the codesign batching."""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

from relocator_helpers import (
    _keg_with_dylib,
    _lc_dylib,
    _relocate_tree,
)

from brewery.providers.relocator import macho as macho_mod
from brewery.providers.relocator import tools as tools_mod
from brewery.providers.relocator.macho import InstallName, NameKind


class TestWritable:
    """Tests for the borrowed owner-write bit, which is per inode, not per path."""

    def _hard_linked_pair(self, tmp_path: Path) -> tuple[Path, Path]:
        """Test that two names for one read-only file, as qtbase ships qmake and qmake6.

        Args:
            tmp_path: The pytest temp dir.

        Returns:
            The (first, second) paths sharing an inode.
        """
        first = tmp_path / "qmake"
        first.write_bytes(b"binary")
        second = tmp_path / "qmake6"
        os.link(first, second)
        os.chmod(first, 0o555)

        return first, second

    def test_a_peer_leaving_does_not_strip_a_held_write_bit(self, tmp_path) -> None:
        """Test that the bit survives until the last holder of the inode leaves."""
        first, second = self._hard_linked_pair(tmp_path)

        entered = threading.Event()
        release = threading.Event()
        left = threading.Event()
        observed: list[bool] = []

        def peer() -> None:
            """Borrow the bit, hold it until told, then leave first."""
            with tools_mod._writable(first):
                entered.set()
                release.wait(5)

            left.set()

        def holder() -> None:
            """Join the borrow, outlive the peer, then check the bit is still set."""
            entered.wait(5)
            with tools_mod._writable(second):
                release.set()
                left.wait(5)
                observed.append(bool(second.stat().st_mode & 0o200))

        threads = [threading.Thread(target=peer), threading.Thread(target=holder)]
        for t in threads:
            t.start()

        for t in threads:
            t.join(10)

        assert observed == [True], "the write bit was restored while still in use"
        assert oct(first.stat().st_mode & 0o777) == "0o555"  # Restored once, at the end
        assert oct(second.stat().st_mode & 0o777) == "0o555"

    def test_a_stale_mode_cannot_skip_the_hold(self, tmp_path, monkeypatch) -> None:
        """Test that the bit cannot be skipped over by a stale mode."""
        first, second = self._hard_linked_pair(tmp_path)

        peer = contextlib.ExitStack()
        peer.enter_context(tools_mod._writable(first))

        fired: list[bool] = []
        observed: list[bool] = []
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            """Land the peer's restore between the joiner's stat and its decision."""
            st = real_stat(path, *args, **kwargs)
            if (
                str(path) == str(second)
                and not fired
                and tools_mod._MODE_GUARD.acquire(False)
            ):
                tools_mod._MODE_GUARD.release()
                fired.append(True)
                peer.close()

            return st

        monkeypatch.setattr(os, "stat", stat)

        with tools_mod._writable(second):
            observed.append(bool(real_stat(second).st_mode & 0o200))

        peer.close()  # A no-op unless the guard held and the peer never left

        assert observed == [True], "a stale mode left the joiner without the write bit"
        assert oct(real_stat(first).st_mode & 0o777) == "0o555"
        assert oct(real_stat(second).st_mode & 0o777) == "0o555"

    def test_nested_holds_on_one_inode_restore_once(self, tmp_path) -> None:
        """Test that the batched re-sign takes both names of a hard link in one ExitStack."""
        first, second = self._hard_linked_pair(tmp_path)

        with tools_mod._writable(first):
            with tools_mod._writable(second):
                assert first.stat().st_mode & 0o200

            assert first.stat().st_mode & 0o200  # Outer hold still relies on it

        assert oct(first.stat().st_mode & 0o777) == "0o555"

    def test_a_name_whose_file_was_replaced_is_still_restored(self, tmp_path) -> None:
        """Test that `codesign` replaces the file it signs, so a name outlives its inode."""
        first, second = self._hard_linked_pair(tmp_path)

        with tools_mod._writable(first), tools_mod._writable(second):
            for p in (first, second):
                # What signing does: a fresh file renamed over the name, taking
                # the borrowed mode with it and breaking the link
                new = p.with_name(p.name + ".signed")
                new.write_bytes(b"signed")
                os.chmod(new, 0o755)
                os.replace(new, p)

        assert oct(first.stat().st_mode & 0o777) == "0o555"
        assert oct(second.stat().st_mode & 0o777) == "0o555"

    def test_an_already_writable_file_is_left_alone(self, tmp_path) -> None:
        """Test that nothing to borrow and nothing to restore, so no bookkeeping is kept."""
        p = tmp_path / "rw"
        p.write_bytes(b"x")
        os.chmod(p, 0o644)

        with tools_mod._writable(p):
            assert oct(p.stat().st_mode & 0o777) == "0o644"

        assert oct(p.stat().st_mode & 0o777) == "0o644"
        assert not tools_mod._MADE_WRITABLE

    def test_the_registry_empties_after_every_hold(self, tmp_path) -> None:
        """Test that a hold that leaks would keep a keg's file writable for the whole run."""
        first, _ = self._hard_linked_pair(tmp_path)

        with tools_mod._writable(first):
            assert tools_mod._MADE_WRITABLE

        assert not tools_mod._MADE_WRITABLE

    def test_both_names_of_a_hard_link_are_rewritten(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Test that end to end: a keg whose two names share one read-only Mach-O inode."""
        mock_run()
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [
                _lc_dylib(
                    macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libtwin.dylib"
                )
            ],
            name="libtwin.dylib",
        )
        twin = dylib.with_name("libtwin6.dylib")
        os.link(dylib, twin)
        os.chmod(dylib, 0o555)

        _relocate_tree(keg, **brew_paths)

        expected = [InstallName(NameKind.ID, "/opt/homebrew/lib/libtwin.dylib")]
        assert macho_mod.find_install_names(dylib) == expected
        assert macho_mod.find_install_names(twin) == expected
        assert oct(dylib.stat().st_mode & 0o777) == "0o555"
        assert oct(twin.stat().st_mode & 0o777) == "0o555"


class TestSignOrder:
    """Tests for the codesign batch ordering."""

    def test_nested_bundle_is_signed_before_its_container(self) -> None:
        """Test that signing a framework validates the code nested inside it.

        qtwebengine's real shape: a helper .app inside the framework whose main
        binary is also in the batch.
        """
        fw = Path("/keg/lib/QtWebEngineCore.framework/Versions/A")
        container = fw / "QtWebEngineCore"
        nested = fw / "Helpers/QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess"

        for batch in ([container, nested], [nested, container]):
            ordered = tools_mod._sign_order(batch)
            assert ordered.index(nested) < ordered.index(container)

    def test_order_is_deterministic_regardless_of_input_order(self) -> None:
        """Test that the batch must not depend on which worker finished first."""
        paths = [
            Path("/keg/lib/libb.dylib"),
            Path("/keg/lib/A.framework/Versions/A/A"),
            Path("/keg/lib/liba.dylib"),
            Path("/keg/bin/tool"),
        ]
        assert tools_mod._sign_order(paths) == tools_mod._sign_order(
            list(reversed(paths))
        )

    def test_every_path_is_kept_exactly_once(self) -> None:
        """Test that ordering is a permutation, not a filter."""
        paths = [Path(f"/keg/lib/lib{i}.dylib") for i in range(5)]
        assert sorted(tools_mod._sign_order(paths)) == sorted(paths)

    def test_empty_batch(self) -> None:
        """Test that no paths means no ordering work."""
        assert tools_mod._sign_order([]) == []


class TestChunkPaths:
    """Tests for the codesign argv chunker."""

    def test_single_chunk_under_budget(self) -> None:
        """Test that a handful of short paths stay in one chunk."""
        paths = [Path(f"/keg/lib/lib{i}.dylib") for i in range(5)]
        assert tools_mod._chunk_paths(paths, budget=1024) == [paths]

    def test_splits_when_byte_budget_exceeded(self) -> None:
        """Test that paths are split into multiple chunks once the byte budget is hit."""
        paths = [Path("/keg/lib/" + "x" * 40 + f"{i}.dylib") for i in range(10)]
        chunks = tools_mod._chunk_paths(paths, budget=100)

        assert len(chunks) > 1
        # Every input path appears exactly once, order preserved
        assert [p for chunk in chunks for p in chunk] == paths
        # No chunk (beyond a lone oversized path) exceeds the budget
        for chunk in chunks:
            if len(chunk) > 1:
                assert sum(len(str(p).encode()) + 1 for p in chunk) <= 100

    def test_oversized_single_path_gets_own_chunk(self) -> None:
        """Test that oversized single paths get their own chunk."""
        paths = [Path("/keg/" + "y" * 500 + ".dylib")]
        assert tools_mod._chunk_paths(paths, budget=100) == [paths]

    def test_empty_input(self) -> None:
        """Test that no paths means no chunks."""
        assert tools_mod._chunk_paths([], budget=100) == []
