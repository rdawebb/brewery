"""Unit tests for ELF parsing and patchelf argument building."""

from __future__ import annotations

from pathlib import Path

import pytest
from relocator_helpers import (
    _elf64,
    _keg_with_elf,
    _relocate_tree,
)

from brewery.core.errors import RelocationError
from brewery.providers.relocator import elf as elf_mod
from brewery.providers.relocator import reader as reader_mod


class TestElfParsing:
    """Tests for the in-process ELF reader."""

    def _read(self, tmp_path: Path, data: bytes) -> elf_mod._ElfInfo:
        """Write ELF bytes to a file and parse them through a real descriptor.

        Args:
            tmp_path: The pytest temp dir.
            data: The ELF image bytes.

        Returns:
            The parsed _ElfInfo.
        """
        p = tmp_path / "bin"
        p.write_bytes(data)
        with p.open("rb") as fh:
            return elf_mod._read_elf(reader_mod._Reader(fh.fileno(), len(data)))

    def test_reads_interp_rpath_runpath(self, tmp_path) -> None:
        """Tests that PT_INTERP and DT_RPATH/DT_RUNPATH strings are parsed."""
        info = self._read(
            tmp_path,
            _elf64(
                interp="@@HOMEBREW_PREFIX@@/lib/ld.so",
                rpath="@@HOMEBREW_PREFIX@@/lib",
                runpath="@@HOMEBREW_CELLAR@@/foo/1.0/lib",
            ),
        )

        assert info.interp == "@@HOMEBREW_PREFIX@@/lib/ld.so"
        assert info.rpath == "@@HOMEBREW_PREFIX@@/lib"
        assert info.runpath == "@@HOMEBREW_CELLAR@@/foo/1.0/lib"

    def test_big_endian_branch(self, tmp_path) -> None:
        """Tests that a big-endian ELF is parsed via the swapped byte order."""
        info = self._read(
            tmp_path, _elf64(rpath="@@HOMEBREW_PREFIX@@/lib", big_endian=True)
        )

        assert info.rpath == "@@HOMEBREW_PREFIX@@/lib"
        assert info.runpath is None


class TestElfRewrite:
    """Tests for ELF relocation, driven through the real keg walker."""

    def test_builds_correct_patchelf_argv(self, tmp_path, brew_paths, mock_run) -> None:
        """Tests that patchelf rewrites interpreter and rpath, and no codesign runs."""
        runs = mock_run()
        keg, elf = _keg_with_elf(
            tmp_path,
            interp="@@HOMEBREW_PREFIX@@/lib/ld.so",
            rpath="@@HOMEBREW_PREFIX@@/lib:@@HOMEBREW_CELLAR@@/foo/1.0/lib",
        )

        result = _relocate_tree(keg, **brew_paths)
        assert result.elf_relocated == 1
        assert result.macho_relocated == 0
        assert len(runs) == 1  # patchelf only; codesign batch is empty on Linux

        cmd = runs[0]
        assert cmd[0] == "patchelf"
        assert str(elf) == cmd[-1]
        assert cmd[cmd.index("--set-interpreter") + 1] == "/opt/homebrew/lib/ld.so"
        assert (
            cmd[cmd.index("--set-rpath") + 1]
            == "/opt/homebrew/lib:/opt/homebrew/Cellar/foo/1.0/lib"
        )

    def test_runpath_preferred_over_rpath(self, tmp_path, brew_paths, mock_run) -> None:
        """Tests that DT_RUNPATH wins over DT_RPATH when both are present."""
        runs = mock_run()
        _keg_with_elf(
            tmp_path,
            rpath="@@HOMEBREW_PREFIX@@/oldlib",
            runpath="@@HOMEBREW_PREFIX@@/newlib",
        )
        _relocate_tree(tmp_path / "keg", **brew_paths)

        cmd = runs[0]
        assert cmd[cmd.index("--set-rpath") + 1] == "/opt/homebrew/newlib"

    def test_skip_relocation_leaves_elf_untouched(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Tests that :any_skip_relocation skips the patchelf rewrite."""
        runs = mock_run()
        _keg_with_elf(tmp_path, rpath="@@HOMEBREW_PREFIX@@/lib")
        result = _relocate_tree(tmp_path / "keg", **brew_paths, skip_relocation=True)

        assert result.elf_relocated == 0
        assert runs == []

    def test_marker_outside_linkage_is_noop(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Tests that a placeholder only in a non-linkage string is not patched."""
        runs = mock_run()
        _keg_with_elf(
            tmp_path,
            rpath="/plain/lib",  # No placeholder
            extra="@@HOMEBREW_PREFIX@@/embedded",  # Marker present, but unreferenced
        )
        result = _relocate_tree(tmp_path / "keg", **brew_paths)

        assert result.elf_relocated == 0
        assert runs == []  # Nothing to rewrite

    def test_unresolved_placeholder_raises(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Tests that a surviving placeholder in an rpath aborts the relocation."""
        mock_run()
        _keg_with_elf(tmp_path, rpath="@@HOMEBREW_PREFIX@@/lib:@@HOMEBREW_MISSING@@")
        with pytest.raises(RelocationError, match="unresolved placeholder"):
            _relocate_tree(tmp_path / "keg", **brew_paths)
