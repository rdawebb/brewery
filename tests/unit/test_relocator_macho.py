"""Unit tests for Mach-O parsing and install-name rewriting."""

from __future__ import annotations

import os
import struct

import pytest
from relocator_helpers import (
    _CPU_ARM64,
    _MH_DYLIB,
    _fat_macho,
    _keg_with_dylib,
    _lc_dylib,
    _lc_rpath,
    _relocate_tree,
    _slots,
    _thin_macho,
)

from brewery.core.errors import RelocationError
from brewery.providers.relocator import macho as macho_mod
from brewery.providers.relocator import reader as reader_mod
from brewery.providers.relocator import tools as tools_mod
from brewery.providers.relocator.macho import InstallName, NameKind

pytestmark = pytest.mark.unit


class TestMachODetection:
    """Tests for Mach-O file detection."""

    @pytest.mark.parametrize(
        "magic",
        [
            macho_mod._MH_MAGIC_64,
            macho_mod._MH_MAGIC,
            macho_mod._MH_CIGAM_64,
            macho_mod._MH_CIGAM,
            macho_mod._FAT_MAGIC,
            macho_mod._FAT_CIGAM,
            macho_mod._FAT_MAGIC_64,
            macho_mod._FAT_CIGAM_64,
        ],
    )
    def test_is_macho_true_for_all_magics(self, tmp_path, magic) -> None:
        """Tests that all valid Mach-O magic numbers are recognised."""
        p = tmp_path / "bin"
        p.write_bytes(struct.pack(">I", magic) + b"\x00" * 64)
        assert macho_mod.is_macho(p)

    def test_is_macho_false_for_script_and_short_file(self, tmp_path) -> None:
        """Tests that non-Mach-O files are not recognised."""
        (tmp_path / "s").write_bytes(b"#!/bin/sh\necho hi\n")
        (tmp_path / "tiny").write_bytes(b"\xfe\xed")
        assert not macho_mod.is_macho(tmp_path / "s")
        assert not macho_mod.is_macho(tmp_path / "tiny")


class TestMachOParsing:
    """Tests for Mach-O parsing."""

    def test_parse_thin_little_endian(self, tmp_path) -> None:
        """Tests parsing of a thin Mach-O binary (little-endian)."""
        macho = _thin_macho(
            [
                _lc_dylib(
                    macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"
                ),
                _lc_dylib(
                    macho_mod._LC_LOAD_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libbar.dylib"
                ),
                _lc_dylib(macho_mod._LC_LOAD_WEAK_DYLIB, "/usr/lib/libSystem.B.dylib"),
                _lc_rpath("@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
            ]
        )
        p = tmp_path / "libfoo.dylib"
        p.write_bytes(macho)

        names = macho_mod.find_install_names(p)
        assert names == [
            InstallName(NameKind.ID, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
            InstallName(NameKind.DYLIB, "@@HOMEBREW_PREFIX@@/lib/libbar.dylib"),
            InstallName(NameKind.DYLIB, "/usr/lib/libSystem.B.dylib"),
            InstallName(NameKind.RPATH, "@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
        ]

    def test_parse_thin_reads_load_commands_past_the_head_window(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test that a header larger than the prefetched window is read the rest of the way.

        Real dylibs carry more load commands than any window worth prefetching,
        so the pread fallback has to produce the same slots as the cached path.
        """
        cmds = [
            _lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
            *(
                _lc_dylib(macho_mod._LC_LOAD_DYLIB, f"/usr/lib/lib{i}.dylib")
                for i in range(40)
            ),
        ]
        p = tmp_path / "libfoo.dylib"
        p.write_bytes(_thin_macho(cmds))

        expected = macho_mod.find_install_names(p)
        monkeypatch.setattr(reader_mod, "_HEAD_WINDOW", 16)

        assert macho_mod.find_install_names(p) == expected
        assert len(expected) == 41

    def test_parse_fat_reads_a_slice_past_the_head_window(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test that a fat slice sits at an arbitrary offset, so it is never in the window."""
        monkeypatch.setattr(reader_mod, "_HEAD_WINDOW", 16)
        slice_ = _thin_macho(
            [_lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/x")]
        )
        p = tmp_path / "x"
        p.write_bytes(_fat_macho([slice_, slice_]))

        assert macho_mod.find_install_names(p) == [
            InstallName(NameKind.ID, "@@HOMEBREW_PREFIX@@/lib/x")
        ]

    def test_slots_of_a_fat_binary_point_at_the_real_file_offsets(
        self, tmp_path
    ) -> None:
        """Test that slot offsets stay absolute, since `_patch_macho` writes back to them."""
        name = "@@HOMEBREW_PREFIX@@/lib/x"
        slice_ = _thin_macho([_lc_dylib(macho_mod._LC_ID_DYLIB, name)])
        p = tmp_path / "x"
        raw = _fat_macho([slice_, slice_])
        p.write_bytes(raw)

        slots = _slots(p)
        assert len(slots) == 2
        for slot in slots:
            assert raw[slot.offset : slot.offset + len(name)] == name.encode()
            assert slot.offset < slot.limit <= len(raw)

    def test_implausible_sizeofcmds_is_rejected(self, tmp_path) -> None:
        """Test that a header claiming a huge command block is malformed, not something to read in."""
        header = struct.pack(
            "<IiiIIIII",
            macho_mod._MH_MAGIC_64,
            _CPU_ARM64,
            0,
            _MH_DYLIB,
            1,
            1 << 30,
            0,
            0,
        )
        p = tmp_path / "libbad.dylib"
        p.write_bytes(header)

        with pytest.raises(ValueError):
            macho_mod.find_install_names(p)

    def test_parse_thin_big_endian_branch(self, tmp_path) -> None:
        """Tests parsing of a thin Mach-O binary (big-endian)."""
        # Guards the ppc/swapped path so the byte-order detection can't collapse to a single branch
        macho = _thin_macho(
            [
                _lc_dylib(
                    macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/x.dylib", bo=">"
                )
            ],
            big_endian=True,
        )
        p = tmp_path / "x.dylib"
        p.write_bytes(macho)
        names = macho_mod.find_install_names(p)
        assert names == [InstallName(NameKind.ID, "@@HOMEBREW_PREFIX@@/lib/x.dylib")]

    def test_parse_fat_dedupes_across_slices(self, tmp_path) -> None:
        """Tests parsing of a fat Mach-O binary (deduplication across slices)."""
        slice_ = _thin_macho(
            [
                _lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libu.dylib"),
            ]
        )
        p = tmp_path / "fat.dylib"
        p.write_bytes(_fat_macho([slice_, slice_]))
        names = macho_mod.find_install_names(p)

        # Same install name in both arch slices collapses to one entry
        assert names == [InstallName(NameKind.ID, "@@HOMEBREW_PREFIX@@/lib/libu.dylib")]

    def test_slots_locate_the_padded_string_region(self, tmp_path) -> None:
        """Tests each slot's [offset, limit) must span the string and its NUL padding."""
        p = tmp_path / "libfoo.dylib"
        p.write_bytes(
            _thin_macho(
                [
                    _lc_dylib(
                        macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"
                    ),
                    _lc_rpath("@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
                ]
            )
        )
        raw = p.read_bytes()

        for slot in _slots(p):
            region = raw[slot.offset : slot.limit]
            assert region.startswith(slot.value.encode())
            # Everything past the string is padding, so the region is writable
            assert set(region[len(slot.value) :]) == {0}

    def test_fat_slots_keep_every_slice(self, tmp_path) -> None:
        """Tests a name shared by two slices is two patch sites, not one."""
        slice_ = _thin_macho(
            [_lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/u.dylib")]
        )
        p = tmp_path / "fat.dylib"
        p.write_bytes(_fat_macho([slice_, slice_]))

        slots = _slots(p)
        assert [s.value for s in slots] == ["@@HOMEBREW_PREFIX@@/lib/u.dylib"] * 2
        assert slots[0].offset != slots[1].offset

    def test_parse_empty_file(self, tmp_path) -> None:
        """Tests parsing of an empty Mach-O file."""
        p = tmp_path / "empty"
        p.write_bytes(b"")
        assert macho_mod.find_install_names(p) == []


class TestMachORewrite:
    """Tests for Mach-O relocation, driven through the real keg walker."""

    def test_install_names_are_rewritten_in_place(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Tests the in-process rewriter patches the header without spawning a tool."""
        runs = mock_run()
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [
                _lc_dylib(
                    macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"
                ),
                _lc_dylib(
                    macho_mod._LC_LOAD_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libbar.dylib"
                ),
                _lc_dylib(
                    macho_mod._LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"
                ),  # untouched
                _lc_rpath("@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
            ],
        )
        before = dylib.stat().st_size

        result = _relocate_tree(keg, **brew_paths)
        assert result.macho_relocated == 1

        # Only the batched re-sign; install_name_tool never ran
        assert [cmd[0] for cmd in runs] == ["codesign"]

        assert [(n.kind, n.value) for n in macho_mod.find_install_names(dylib)] == [
            (NameKind.ID, "/opt/homebrew/lib/libfoo.dylib"),
            (NameKind.DYLIB, "/opt/homebrew/lib/libbar.dylib"),
            (NameKind.DYLIB, "/usr/lib/libSystem.B.dylib"),
            (NameKind.RPATH, "/opt/homebrew/Cellar/foo/1.0/lib"),
        ]

        # The layout is untouched and no stale tail survives
        assert dylib.stat().st_size == before
        assert b"@@HOMEBREW" not in dylib.read_bytes()

    def test_every_fat_slice_is_patched(self, tmp_path, brew_paths, mock_run) -> None:
        """Tests a name shared across slices must be rewritten in both of them."""
        mock_run()
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        dylib = keg / "lib" / "fat.dylib"
        slice_ = _thin_macho(
            [_lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/fat.dylib")]
        )
        dylib.write_bytes(_fat_macho([slice_, slice_]))

        assert _relocate_tree(keg, **brew_paths).macho_relocated == 1
        assert [s.value for s in _slots(dylib)] == ["/opt/homebrew/lib/fat.dylib"] * 2

    def test_falls_back_when_name_outgrows_its_padding(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Tests a replacement too long for its load command goes to install_name_tool."""
        runs = mock_run()
        keg, dylib = _keg_with_dylib(
            tmp_path, [_lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_CELLAR@@/lib")]
        )
        original = dylib.read_bytes()

        assert _relocate_tree(keg, **brew_paths).macho_relocated == 1
        assert [cmd[0] for cmd in runs] == ["install_name_tool", "codesign"]
        assert runs[0][:3] == ["install_name_tool", "-id", "/opt/homebrew/Cellar/lib"]

        # The stubbed tool is a no-op, so the file must be exactly as it was
        assert dylib.read_bytes() == original

    def test_falls_back_when_verification_fails(
        self, tmp_path, brew_paths, mock_run, monkeypatch
    ) -> None:
        """Tests a patch that does not verify is rolled back, then retried with the tool."""
        runs = mock_run()
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [_lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib")],
        )
        original = dylib.read_bytes()
        monkeypatch.setattr(macho_mod, "_verify_macho", lambda path: False)

        assert _relocate_tree(keg, **brew_paths).macho_relocated == 1
        assert [cmd[0] for cmd in runs] == ["install_name_tool", "codesign"]
        assert dylib.read_bytes() == original  # Rolled back before the fallback

    def test_kill_switch_forces_install_name_tool(
        self, tmp_path, brew_paths, mock_run, force_install_name_tool
    ) -> None:
        """Tests BREWERY_NO_NATIVE_MACHO routes even a fitting rewrite to the tool."""
        runs = mock_run()
        keg, _ = _keg_with_dylib(
            tmp_path,
            [_lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib")],
        )

        assert _relocate_tree(keg, **brew_paths).macho_relocated == 1
        assert [cmd[0] for cmd in runs] == ["install_name_tool", "codesign"]

    def test_fallback_builds_correct_macho_argv(
        self, tmp_path, brew_paths, mock_run, force_install_name_tool
    ) -> None:
        """Tests that the correct arguments are passed to the relocation commands."""
        runs = mock_run()
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [
                _lc_dylib(
                    macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"
                ),
                _lc_dylib(
                    macho_mod._LC_LOAD_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libbar.dylib"
                ),
                _lc_dylib(
                    macho_mod._LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"
                ),  # untouched
                _lc_rpath("@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
            ],
        )

        result = _relocate_tree(keg, **brew_paths)
        assert result.macho_relocated == 1
        assert len(runs) == 2  # install_name_tool, then a single batched codesign

        int_cmd = runs[0]
        assert int_cmd[0] == "install_name_tool"
        assert str(dylib) == int_cmd[-1]

        # -id uses new only; -change uses old+new; -rpath uses old+new; libSystem absent
        assert "-id" in int_cmd
        assert int_cmd[int_cmd.index("-id") + 1] == "/opt/homebrew/lib/libfoo.dylib"
        assert [
            "-change",
            "@@HOMEBREW_PREFIX@@/lib/libbar.dylib",
            "/opt/homebrew/lib/libbar.dylib",
        ] == self._slice_flag(int_cmd, "-change")
        assert [
            "-rpath",
            "@@HOMEBREW_CELLAR@@/foo/1.0/lib",
            "/opt/homebrew/Cellar/foo/1.0/lib",
        ] == self._slice_flag(int_cmd, "-rpath")
        assert "/usr/lib/libSystem.B.dylib" not in int_cmd

        sign_cmd = runs[1]
        assert sign_cmd[0] == "codesign"
        assert "--force" in sign_cmd and "-" in sign_cmd

    def _slice_flag(self, argv: list[str], flag: str) -> list[str]:
        """Returns the arguments for a specific flag from the command line.

        Args:
            argv: The command line arguments.
            flag: The flag to slice.

        Returns:
            A list of arguments for the specified flag.
        """
        i = argv.index(flag)

        return argv[i : i + 3]

    def test_noop_when_no_placeholders(self, tmp_path, brew_paths, mock_run) -> None:
        """Tests that no changes are made when there are no placeholders."""
        runs = mock_run()
        keg, _ = _keg_with_dylib(
            tmp_path, [_lc_dylib(macho_mod._LC_ID_DYLIB, "/usr/lib/libSystem.B.dylib")]
        )

        # No placeholders -> no rewrite, and no re-sign
        result = _relocate_tree(keg, **brew_paths)
        assert result.macho_relocated == 0
        assert runs == []

    def test_placeholder_outside_the_install_names_is_ignored(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Test that a marker in the body is not an install name, so the file is left alone.

        The dispatch reads the load commands and never scans the body, so this is
        the case that would regress if it started substituting binary content.
        """
        runs = mock_run()
        keg, dylib = _keg_with_dylib(
            tmp_path, [_lc_dylib(macho_mod._LC_ID_DYLIB, "/usr/lib/libSystem.B.dylib")]
        )
        body = dylib.read_bytes() + b"@@HOMEBREW_PREFIX@@/nope\x00"
        dylib.write_bytes(body)

        result = _relocate_tree(keg, **brew_paths)
        assert result.macho_relocated == 0
        assert runs == []
        assert dylib.read_bytes() == body

    def test_mach_o_with_an_unparseable_header_is_skipped(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Test that a truncated fat header is skipped, not raised through the pool.

        Every Mach-O reaches the parser now, not only the ones holding a marker,
        so `struct.error` from a malformed header has to be caught.
        """
        runs = mock_run()
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        broken = keg / "lib" / "libtruncated.dylib"

        # Fat magic claiming 64 slices, with none of the arch table present
        broken.write_bytes(
            struct.pack(">II", macho_mod._FAT_MAGIC, 64) + b"@@HOMEBREW_PREFIX@@\x00"
        )

        result = _relocate_tree(keg, **brew_paths)
        assert result.macho_relocated == 0
        assert runs == []

    def test_install_name_tool_failure_raises_relocation_error(
        self, tmp_path, brew_paths, mock_run, force_install_name_tool
    ) -> None:
        """Test that a failing install_name_tool aborts with the offending file and reason."""
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [_lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib")],
        )

        mock_run(stderr="load command too large", returncode=1)
        with pytest.raises(RelocationError) as exc:
            _relocate_tree(keg, **brew_paths)
        assert exc.value.path == dylib
        assert "too large" in exc.value.reason

    def test_relocate_macho_handles_readonly_binary(
        self, tmp_path, brew_paths, monkeypatch
    ) -> None:
        """Tests that Mach-O relocation handles read-only binaries correctly."""
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [_lc_dylib(macho_mod._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libro.dylib")],
            name="libro.dylib",
        )
        os.chmod(dylib, 0o555)  # Typical read-only executable mode in a keg

        # codesign must see a writable file
        seen_mode: list[int] = []

        def record(cmd) -> None:
            """Records the file's write bit at the moment _run is invoked.

            Args:
                cmd: The command that would have been run.
            """
            seen_mode.append(dylib.stat().st_mode & 0o200)

        monkeypatch.setattr(tools_mod, "_run", record)
        result = _relocate_tree(keg, **brew_paths)
        assert result.macho_relocated == 1

        # The in-place write went through despite the missing owner-write bit
        assert macho_mod.find_install_names(dylib) == [
            InstallName(NameKind.ID, "/opt/homebrew/lib/libro.dylib")
        ]
        assert seen_mode and all(seen_mode), "file was not writable during the re-sign"
        assert oct(dylib.stat().st_mode & 0o777) == "0o555"  # Mode restored
