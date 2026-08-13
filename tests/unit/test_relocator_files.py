"""Unit tests for per-file classification and the relocation pool."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from relocator_helpers import (
    _lc_dylib,
    _relocate_tree,
    _thin_macho,
)

from brewery.core.errors import RelocationError
from brewery.providers.relocator import files as files_mod
from brewery.providers.relocator import macho as macho_mod
from brewery.providers.relocator import substitutions as subs_mod
from brewery.providers.relocator import tools as tools_mod

pytestmark = pytest.mark.unit


class TestMarkerScan:
    """Tests for the chunked placeholder scan over a file's body."""

    def _scan(self, tmp_path: Path, data: bytes) -> bool:
        """Write `data` and scan it for the placeholder marker.

        Args:
            tmp_path: The pytest temp dir.
            data: The file's bytes.

        Returns:
            Whether the marker was found.
        """
        p = tmp_path / "f"
        p.write_bytes(data)
        with p.open("rb") as fh:
            return files_mod._has_marker(fh.fileno(), len(data))

    def test_finds_and_misses_a_marker_within_one_chunk(self, tmp_path) -> None:
        """Test that the single-chunk case, which is every file small enough to fit."""
        assert self._scan(tmp_path, b"prefix=@@HOMEBREW_PREFIX@@/opt")
        assert not self._scan(tmp_path, b"prefix=/usr/local/opt")

    def test_finds_a_marker_straddling_a_chunk_boundary(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test that the seam is the one place a chunked scan can lose a match."""
        monkeypatch.setattr(files_mod, "_SCAN_CHUNK", 16)

        # Split the marker across the boundary at byte 16, 4 bytes either side
        data = b"A" * 12 + subs_mod._PLACEHOLDER_MARKER + b"B" * 12
        assert data[12:16] == b"@@HO"
        assert self._scan(tmp_path, data)

    def test_scans_every_chunk_to_the_end(self, tmp_path, monkeypatch) -> None:
        """Test that a marker in the last chunk is found, so the walk cannot stop early."""
        monkeypatch.setattr(files_mod, "_SCAN_CHUNK", 16)
        assert self._scan(tmp_path, b"A" * 64 + subs_mod._PLACEHOLDER_MARKER)


class TestTextRelocation:
    """Tests for the text branch of the per-file worker.

    Symlink targets are substituted in the stream now, as the link is created,
    so those cases live in `tests/integration/test_relocation.py`.
    """

    def test_substitution_preserves_the_exec_bit(self, tmp_path, brew_paths) -> None:
        """Tests that text substitution preserves the executable bit."""
        keg = tmp_path / "keg"
        keg.mkdir()
        p = keg / "foo-config"
        p.write_text(
            "#!/bin/sh\nprefix=@@HOMEBREW_PREFIX@@\nlibs=@@HOMEBREW_CELLAR@@/foo\n"
        )
        os.chmod(p, 0o755)

        assert _relocate_tree(keg, **brew_paths).changed_files == ["foo-config"]
        text = p.read_text()
        assert "prefix=/opt/homebrew" in text
        assert "libs=/opt/homebrew/Cellar/foo" in text
        assert "@@HOMEBREW" not in text
        assert os.stat(p).st_mode & 0o111  # exec bits survived the rewrite

    def test_readonly_file_is_rewritten_and_its_mode_restored(
        self, tmp_path, brew_paths
    ) -> None:
        """Tests that relocation handles read-only files correctly."""
        # Relocation must toggle the write bit and restore the original mode
        keg = tmp_path / "keg"
        keg.mkdir()
        p = keg / "ro-config"
        p.write_text("prefix=@@HOMEBREW_PREFIX@@\n")
        os.chmod(p, 0o444)

        assert _relocate_tree(keg, **brew_paths).changed_files == ["ro-config"]
        assert "prefix=/opt/homebrew" in p.read_text()
        assert oct(p.stat().st_mode & 0o777) == "0o444"  # Mode restored

    def test_marker_free_file_is_untouched(self, tmp_path, brew_paths) -> None:
        """Tests that no changes are made when there are no placeholders."""
        keg = tmp_path / "keg"
        keg.mkdir()
        p = keg / "plain.txt"
        p.write_text("nothing to do here\n")
        before = p.read_bytes()

        assert _relocate_tree(keg, **brew_paths).changed_files == []
        assert p.read_bytes() == before


class TestOrchestration:
    """Tests for orchestration of file relocations."""

    def test_every_kind_of_regular_file_is_handled(
        self, tmp_path, mock_run, brew_paths
    ) -> None:
        """Tests that all file kinds are processed during keg relocation."""
        keg = tmp_path / "keg"
        (keg / "bin").mkdir(parents=True)
        (keg / "lib").mkdir()

        # Text file with placeholder
        (keg / "bin" / "foo-config").write_text("p=@@HOMEBREW_PREFIX@@\n")

        # mach-o with placeholder
        (keg / "lib" / "libfoo.dylib").write_bytes(
            _thin_macho(
                [
                    _lc_dylib(
                        macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"
                    ),
                ]
            )
        )

        # Untouched file
        (keg / "lib" / "data.txt").write_text("no tokens\n")

        mock_run()

        result = _relocate_tree(keg, **brew_paths)

        # Fallback scan: text file and macho modified, data.txt untouched
        assert result.changed_files == ["bin/foo-config"]
        assert result.macho_relocated == 1
        assert "@@HOMEBREW" not in (keg / "bin" / "foo-config").read_text()

    def test_manifest_text_files_gate_the_text_branch(
        self, tmp_path, monkeypatch, brew_paths
    ) -> None:
        """Tests that listed text files are processed and unlisted ones are skipped."""
        keg = tmp_path / "keg"
        (keg / "bin").mkdir(parents=True)
        (keg / "lib" / "pkgconfig").mkdir(parents=True)
        # listed text file (the manifest changed_files entry)
        (keg / "lib" / "pkgconfig" / "foo.pc").write_text(
            "prefix=@@HOMEBREW_PREFIX@@\n"
        )
        # a text file with a placeholder that is NOT listed -> must be left untouched
        (keg / "bin" / "stray").write_text("p=@@HOMEBREW_PREFIX@@\n")
        # a Mach-O is relocated whether or not the tab lists it
        (keg / "lib" / "libfoo.dylib").write_bytes(
            _thin_macho(
                [
                    _lc_dylib(
                        macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"
                    ),
                ]
            )
        )

        monkeypatch.setattr(tools_mod, "_run", lambda cmd: None)

        result = _relocate_tree(keg, **brew_paths, text_files=["lib/pkgconfig/foo.pc"])
        assert result.changed_files == ["lib/pkgconfig/foo.pc"]
        assert result.macho_relocated == 1
        assert "@@HOMEBREW" not in (keg / "lib" / "pkgconfig" / "foo.pc").read_text()

        # The unlisted text file was never read/substituted
        assert (keg / "bin" / "stray").read_text() == "p=@@HOMEBREW_PREFIX@@\n"

    def test_skip_relocation_still_substitutes_text(
        self, tmp_path, mock_run, brew_paths
    ) -> None:
        """Test that skip_relocation maps to brew's skip_linkage.

        Mach-O install names are left alone, but text placeholders are still substituted.
        """
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        (keg / "config").write_text("p=@@HOMEBREW_PREFIX@@\n")
        (keg / "lib" / "libx.dylib").write_bytes(
            _thin_macho(
                [
                    _lc_dylib(
                        macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libx.dylib"
                    )
                ]
            )
        )

        called = mock_run()

        n = _relocate_tree(keg, **brew_paths, skip_relocation=True)

        # Text file substituted; Mach-O linkage skipped (no install_name_tool)
        assert "@@HOMEBREW_PREFIX@@" not in (keg / "config").read_text()
        assert n.macho_relocated == 0
        assert called == []

    def test_formula_tokens_are_substituted(
        self, tmp_path, brew_paths, system_perl
    ) -> None:
        """Test that a perl shebang is rewritten once the formula tokens are supplied."""
        keg = tmp_path / "keg"
        (keg / "libexec" / "bin").mkdir(parents=True)
        script = keg / "libexec" / "bin" / "cloc"
        script.write_text("#!@@HOMEBREW_PERL@@\nprint 1;\n")

        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"],
            name="cloc",
            runtime_deps=[],
            built_on={"preferred_perl": "5.34"},
        )
        result = _relocate_tree(keg, **brew_paths, extra_tokens=tokens)

        assert result.changed_files == ["libexec/bin/cloc"]
        assert script.read_text().startswith("#!/usr/bin/perl5.34\n")

    def test_unresolved_placeholder_aborts(self, tmp_path, brew_paths) -> None:
        """Test that a placeholder with no token in the map must abort, not ship broken."""
        keg = tmp_path / "keg"
        (keg / "libexec" / "bin").mkdir(parents=True)
        (keg / "libexec" / "bin" / "cloc").write_text("#!@@HOMEBREW_PERL@@\n")

        with pytest.raises(RelocationError, match=r"unresolved placeholder .*PERL"):
            _relocate_tree(keg, **brew_paths)

    def test_macho_failure_propagates(
        self, tmp_path, mock_run, brew_paths, force_install_name_tool
    ) -> None:
        """Tests that Mach-O relocation failures are propagated."""
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        (keg / "lib" / "libx.dylib").write_bytes(
            _thin_macho(
                [
                    _lc_dylib(
                        macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libx.dylib"
                    ),
                ]
            )
        )

        mock_run(stderr="load command too large", returncode=1)
        with pytest.raises(RelocationError):
            _relocate_tree(keg, **brew_paths)

    def test_codesign_is_batched_across_machos(
        self, tmp_path, mock_run, brew_paths, force_install_name_tool
    ) -> None:
        """Tests that all rewritten Mach-O files are re-signed in one codesign call, after
        every install_name_tool has run."""
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        names = ["liba.dylib", "libb.dylib", "libc.dylib"]
        for name in names:
            (keg / "lib" / name).write_bytes(
                _thin_macho(
                    [
                        _lc_dylib(
                            macho_mod._LC_ID_DYLIB, f"@@HOMEBREW_PREFIX@@/lib/{name}"
                        )
                    ]
                )
            )

        runs = mock_run()

        result = _relocate_tree(keg, **brew_paths)
        assert result.macho_relocated == 3

        int_runs = [cmd for cmd in runs if cmd[0] == "install_name_tool"]
        sign_runs = [cmd for cmd in runs if cmd[0] == "codesign"]
        assert len(int_runs) == 3  # One per binary
        assert len(sign_runs) == 1  # A single batched re-sign

        signed = {arg for arg in sign_runs[0] if arg.endswith(".dylib")}
        assert signed == {str(keg / "lib" / name) for name in names}

        # codesign must follow every install_name_tool (it strips the signature)
        assert runs.index(sign_runs[0]) == len(runs) - 1

    def test_codesign_failure_propagates(
        self, tmp_path, monkeypatch, brew_paths
    ) -> None:
        """Test that a failing batched codesign aborts the keg with a RelocationError even
        when install_name_tool succeeded."""
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        (keg / "lib" / "libx.dylib").write_bytes(
            _thin_macho(
                [
                    _lc_dylib(
                        macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libx.dylib"
                    )
                ]
            )
        )

        def stub(cmd, *args, **kwargs) -> subprocess.CompletedProcess:
            failed = cmd[0] == "codesign"
            return subprocess.CompletedProcess(
                cmd, 1 if failed else 0, "", "bad signature" if failed else ""
            )

        monkeypatch.setattr(tools_mod.subprocess, "run", stub)

        with pytest.raises(RelocationError, match="codesign failed"):
            _relocate_tree(keg, **brew_paths)
