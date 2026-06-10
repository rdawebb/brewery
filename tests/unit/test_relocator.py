"""Unit tests for the bottle relocation engine."""

from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from brewery.core import shell as shell_mod
from brewery.providers import relocator as r
from brewery.providers.relocator import InstallName, NameKind, RelocationError

pytestmark = pytest.mark.unit

_CPU_ARM64 = 0x0100000C
_MH_DYLIB = 0x6  # Filetype: value is irrelevant to parsing, but realistic


def _lc_dylib(cmd: int, name: str, bo: str = "<") -> bytes:
    """Create a dylib_command structure.

    Args:
        cmd: The command type.
        name: The name of the dynamic library.
        bo: The byte order (default: little-endian).

    Returns:
        The serialized dylib_command structure as bytes.
    """
    name_b = name.encode() + b"\x00"
    name_b += b"\x00" * ((-(24 + len(name_b))) % 8)  # 8-byte align
    cmdsize = 24 + len(name_b)

    # dylib_command: cmd, cmdsize, name.offset=24, timestamp, cur_ver, compat_ver
    return struct.pack(f"{bo}IIIIII", cmd, cmdsize, 24, 0, 0x10000, 0x10000) + name_b


def _lc_rpath(path: str, bo: str = "<") -> bytes:
    """Create an rpath_command structure.

    Args:
        path: The path to the rpath.
        bo: The byte order (default: little-endian).

    Returns:
        The serialized rpath_command structure as bytes.
    """
    p = path.encode() + b"\x00"
    p += b"\x00" * ((-(12 + len(p))) % 8)
    cmdsize = 12 + len(p)

    # rpath_command: cmd, cmdsize, path.offset=12
    return struct.pack(f"{bo}III", r._LC_RPATH, cmdsize, 12) + p


def _thin_macho(load_cmds: list[bytes], *, big_endian: bool = False) -> bytes:
    """Create a thin Mach-O binary.

    Args:
        load_cmds: The load commands to include in the binary.
        big_endian: Whether to use big-endian byte order (default: little-endian).

    Returns:
        The serialized thin Mach-O binary as bytes.
    """
    body = b"".join(load_cmds)
    bo = ">" if big_endian else "<"

    # mach_header_64: magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved
    header = struct.pack(
        f"{bo}IiiIIIII",
        r._MH_MAGIC_64,
        _CPU_ARM64,
        0,
        _MH_DYLIB,
        len(load_cmds),
        len(body),
        0,
        0,
    )

    return header + body


def _fat_macho(slices: list[bytes]) -> bytes:
    """Create a fat Mach-O binary.

    Args:
        slices: The slices to include in the binary.

    Returns:
        The serialized fat Mach-O binary as bytes.
    """
    # fat_header (BE): magic, nfat_arch; then fat_arch[] (BE), then slices.
    nfat = len(slices)
    header = struct.pack(">II", r._FAT_MAGIC, nfat)
    arches = b""

    # First slice starts after header + arch table.
    offset = 8 + nfat * 20
    payload = b""
    for i, sl in enumerate(slices):
        aligned = offset + ((-offset) % 16)
        payload += b"\x00" * (aligned - offset) + sl

        # fat_arch: cputype, cpusubtype, offset, size, align
        arches += struct.pack(">iiIII", _CPU_ARM64 + i, 0, aligned, len(sl), 4)
        offset = aligned + len(sl)

    return header + arches + payload


@pytest.fixture
def subs() -> dict[bytes, bytes]:
    """Fixture for building substitution mappings.

    Returns:
        A dictionary mapping placeholder bytes to their resolved values.
    """
    return r.build_substitutions(
        prefix=Path("/opt/homebrew"),
        cellar=Path("/opt/homebrew/Cellar"),
        repository=Path("/opt/homebrew/Library/Homebrew"),
    )


class TestSubstitution:
    """Tests for the substitution mapping."""

    def test_substitutions_longest_token_first(self, subs) -> None:
        """Tests that the longest token is matched first."""
        # @@HOMEBREW_PREFIX@@ must be matched before @@HOMEBREW_CELLAR@@.
        keys = list(subs.keys())
        assert keys == sorted(keys, key=len, reverse=True)

    def test_apply_replaces_cellar_even_when_longer_than_token(self, subs) -> None:
        """Tests that the substitution is applied even when the replacement is longer."""
        # Cellar expands to a path LONGER than its placeholder
        out = r._apply(b"@@HOMEBREW_CELLAR@@/foo/1.0/lib", subs)
        assert out == b"/opt/homebrew/Cellar/foo/1.0/lib"

    def test_apply_noop_without_marker(self, subs) -> None:
        assert (
            r._apply(b"/usr/lib/libSystem.dylib", subs) == b"/usr/lib/libSystem.dylib"
        )


class TestMachODetection:
    """Tests for Mach-O file detection."""

    @pytest.mark.parametrize(
        "magic",
        [
            r._MH_MAGIC_64,
            r._MH_MAGIC,
            r._MH_CIGAM_64,
            r._MH_CIGAM,
            r._FAT_MAGIC,
            r._FAT_CIGAM,
            r._FAT_MAGIC_64,
            r._FAT_CIGAM_64,
        ],
    )
    def test_is_macho_true_for_all_magics(self, tmp_path, magic) -> None:
        """Tests that all valid Mach-O magic numbers are recognised."""
        p = tmp_path / "bin"
        p.write_bytes(struct.pack(">I", magic) + b"\x00" * 64)
        assert r.is_macho(p)

    def test_is_macho_false_for_script_and_short_file(self, tmp_path) -> None:
        """Tests that non-Mach-O files are not recognised."""
        (tmp_path / "s").write_bytes(b"#!/bin/sh\necho hi\n")
        (tmp_path / "tiny").write_bytes(b"\xfe\xed")
        assert not r.is_macho(tmp_path / "s")
        assert not r.is_macho(tmp_path / "tiny")


class TestMachOParsing:
    """Tests for Mach-O parsing."""

    def test_parse_thin_little_endian(self, tmp_path) -> None:
        """Tests parsing of a thin Mach-O binary (little-endian)."""
        macho = _thin_macho(
            [
                _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
                _lc_dylib(r._LC_LOAD_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libbar.dylib"),
                _lc_dylib(r._LC_LOAD_WEAK_DYLIB, "/usr/lib/libSystem.B.dylib"),
                _lc_rpath("@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
            ]
        )
        p = tmp_path / "libfoo.dylib"
        p.write_bytes(macho)

        names = r.find_install_names(p)
        assert names == [
            InstallName(NameKind.ID, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
            InstallName(NameKind.DYLIB, "@@HOMEBREW_PREFIX@@/lib/libbar.dylib"),
            InstallName(NameKind.DYLIB, "/usr/lib/libSystem.B.dylib"),
            InstallName(NameKind.RPATH, "@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
        ]

    def test_parse_thin_big_endian_branch(self, tmp_path) -> None:
        """Tests parsing of a thin Mach-O binary (big-endian)."""
        # Guards the ppc/swapped path so the byte-order detection can't collapse to a single branch
        macho = _thin_macho(
            [_lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/x.dylib", bo=">")],
            big_endian=True,
        )
        p = tmp_path / "x.dylib"
        p.write_bytes(macho)
        names = r.find_install_names(p)
        assert names == [InstallName(NameKind.ID, "@@HOMEBREW_PREFIX@@/lib/x.dylib")]

    def test_parse_fat_dedupes_across_slices(self, tmp_path) -> None:
        """Tests parsing of a fat Mach-O binary (deduplication across slices)."""
        slice_ = _thin_macho(
            [
                _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libu.dylib"),
            ]
        )
        p = tmp_path / "fat.dylib"
        p.write_bytes(_fat_macho([slice_, slice_]))
        names = r.find_install_names(p)

        # Same install name in both arch slices collapses to one entry
        assert names == [InstallName(NameKind.ID, "@@HOMEBREW_PREFIX@@/lib/libu.dylib")]

    def test_parse_empty_file(self, tmp_path) -> None:
        """Tests parsing of an empty Mach-O file."""
        p = tmp_path / "empty"
        p.write_bytes(b"")
        assert r.find_install_names(p) == []


@pytest.fixture
def fake_run_capture(monkeypatch):
    """Patch shell.run_capture with a recording stub and return its call log.

    Call with no args for a success stub, or pass stderr/returncode to simulate
    a tool failure. The returned list records each argv as it is run.

    Args:
        monkeypatch: The monkeypatch fixture.

    Returns:
        A factory that installs the stub and returns the call-log list.
    """

    def install(
        stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> list[list[str]]:
        runs: list[list[str]] = []

        async def stub(*cmd, timeout=None):
            runs.append(list(cmd))
            return (stdout, stderr, returncode)

        monkeypatch.setattr(shell_mod, "run_capture", stub)
        return runs

    return install


class TestMachORewrite:
    """Tests for Mach-O file relocation."""

    def test_relocate_macho_builds_correct_argv(
        self, tmp_path, subs, fake_run_capture
    ) -> None:
        """Tests that the correct arguments are passed to the relocation commands."""
        runs = fake_run_capture()
        p = tmp_path / "libfoo.dylib"
        p.write_bytes(
            _thin_macho(
                [
                    _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
                    _lc_dylib(r._LC_LOAD_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libbar.dylib"),
                    _lc_dylib(
                        r._LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"
                    ),  # untouched
                    _lc_rpath("@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
                ]
            )
        )

        changed = r.relocate_macho(p, subs)
        assert changed is True
        assert len(runs) == 2  # install_name_tool, then codesign

        int_cmd = runs[0]
        assert int_cmd[0] == "install_name_tool"
        assert str(p) == int_cmd[-1]

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

    def test_relocate_macho_noop_when_no_placeholders(
        self, tmp_path, subs, fake_run_capture
    ) -> None:
        """Tests that no changes are made when there are no placeholders."""
        runs = fake_run_capture()
        p = tmp_path / "clean.dylib"
        p.write_bytes(
            _thin_macho(
                [
                    _lc_dylib(r._LC_ID_DYLIB, "/usr/lib/libSystem.B.dylib"),
                ]
            )
        )

        # No placeholders -> no rewrite, and no re-sign
        assert r.relocate_macho(p, subs) is False
        assert runs == []

    def test_install_name_tool_failure_raises_relocation_error(
        self, tmp_path, subs, fake_run_capture
    ) -> None:
        """Tests that the installation name tool is called with the correct arguments."""
        p = tmp_path / "lib.dylib"
        p.write_bytes(
            _thin_macho(
                [
                    _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/lib.dylib"),
                ]
            )
        )

        fake_run_capture(stderr="load command too large", returncode=1)
        with pytest.raises(RelocationError) as exc:
            r.relocate_macho(p, subs)
        assert exc.value.path == p
        assert "too large" in exc.value.reason


class TestTextSymlinkRelocation:
    """Tests for text file and symlink relocation."""

    def test_relocate_text_substitutes_and_preserves_exec_bit(
        self, tmp_path, subs
    ) -> None:
        """Tests that text substitution preserves the executable bit."""
        p = tmp_path / "foo-config"
        p.write_text(
            "#!/bin/sh\nprefix=@@HOMEBREW_PREFIX@@\nlibs=@@HOMEBREW_CELLAR@@/foo\n"
        )
        os.chmod(p, 0o755)

        assert r.relocate_text(p, subs) is True
        text = p.read_text()
        assert "prefix=/opt/homebrew" in text
        assert "libs=/opt/homebrew/Cellar/foo" in text
        assert "@@HOMEBREW" not in text
        assert os.stat(p).st_mode & 0o111  # exec bits survived the rewrite

    def test_relocate_text_handles_readonly_file(self, tmp_path, subs) -> None:
        """Tests that relocation handles read-only files correctly."""
        # Relocation must toggle the write bit and restore the original mode
        p = tmp_path / "ro-config"
        p.write_text("prefix=@@HOMEBREW_PREFIX@@\n")
        os.chmod(p, 0o444)

        assert r.relocate_text(p, subs) is True
        assert "prefix=/opt/homebrew" in p.read_text()
        assert oct(p.stat().st_mode & 0o777) == "0o444"  # Mode restored

    def test_relocate_macho_handles_readonly_binary(
        self, tmp_path, subs, monkeypatch
    ) -> None:
        """Tests that Mach-O relocation handles read-only binaries correctly."""
        p = tmp_path / "libro.dylib"
        p.write_bytes(
            _thin_macho(
                [
                    _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libro.dylib"),
                ]
            )
        )
        os.chmod(p, 0o555)  # Typical read-only executable mode in a keg

        # Inside _run the file must be writable
        seen_mode: list[int] = []

        def record(cmd):
            """Records the file's write bit at the moment _run is invoked.

            Args:
                cmd: The command that would have been run.
            """
            seen_mode.append(p.stat().st_mode & 0o200)

        monkeypatch.setattr(r, "_run", record)
        assert r.relocate_macho(p, subs) is True

        assert seen_mode and all(seen_mode), "file was not writable during the rewrite"
        assert oct(p.stat().st_mode & 0o777) == "0o555"  # Mode restored

    def test_relocate_text_noop_without_marker(self, tmp_path, subs) -> None:
        """Tests that no changes are made when there are no placeholders."""
        p = tmp_path / "plain.txt"
        p.write_text("nothing to do here\n")
        before = p.read_bytes()
        assert r.relocate_text(p, subs) is False
        assert p.read_bytes() == before

    def test_relocate_symlink_rewrites_placeholder_target(self, tmp_path, subs) -> None:
        """Tests that symlink relocation rewrites the target correctly."""
        link = tmp_path / "link"
        os.symlink("@@HOMEBREW_PREFIX@@/bin/real", link)
        assert r.relocate_symlink(link, subs) is True
        assert os.readlink(link) == "/opt/homebrew/bin/real"

    def test_relocate_symlink_noop_for_plain_target(self, tmp_path, subs) -> None:
        """Tests that symlink relocation is a no-op for plain targets."""
        link = tmp_path / "link"
        os.symlink("../relative/target", link)
        assert r.relocate_symlink(link, subs) is False
        assert os.readlink(link) == "../relative/target"


class TestOrchestration:
    """Tests for orchestration of file relocations."""

    def test_relocate_keg_walks_all_file_kinds(
        self, tmp_path, fake_run_capture
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
                    _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
                ]
            )
        )

        # Symlink with placeholder target
        os.symlink("@@HOMEBREW_PREFIX@@/bin/foo", keg / "bin" / "foo")

        # Untouched file
        (keg / "lib" / "data.txt").write_text("no tokens\n")

        fake_run_capture()

        modified = r.relocate_keg(
            keg,
            prefix=Path("/opt/homebrew"),
            cellar=Path("/opt/homebrew/Cellar"),
            repository=Path("/opt/homebrew/Library/Homebrew"),
        )

        # Text + mach-o + symlink modified; data.txt not counted
        assert modified == 3
        assert "@@HOMEBREW" not in (keg / "bin" / "foo-config").read_text()
        assert os.readlink(keg / "bin" / "foo") == "/opt/homebrew/bin/foo"

    def test_relocate_keg_skip_relocation_is_noop(
        self, tmp_path, fake_run_capture
    ) -> None:
        """Tests that skipping relocation is a no-op."""
        keg = tmp_path / "keg"
        keg.mkdir()
        (keg / "config").write_text("p=@@HOMEBREW_PREFIX@@\n")

        called = fake_run_capture()

        n = r.relocate_keg(
            keg,
            prefix=Path("/opt/homebrew"),
            cellar=Path("/opt/homebrew/Cellar"),
            repository=Path("/opt/homebrew/Library/Homebrew"),
            skip_relocation=True,
        )
        assert n == 0
        assert called == []

        # File left untouched because :any_skip_relocation bottles need no work
        assert "@@HOMEBREW_PREFIX@@" in (keg / "config").read_text()

    def test_relocate_keg_propagates_macho_failure(
        self, tmp_path, fake_run_capture
    ) -> None:
        """Tests that Mach-O relocation failures are propagated."""
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        (keg / "lib" / "libx.dylib").write_bytes(
            _thin_macho(
                [
                    _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libx.dylib"),
                ]
            )
        )

        fake_run_capture(stderr="load command too large", returncode=1)
        with pytest.raises(RelocationError):
            r.relocate_keg(
                keg,
                prefix=Path("/opt/homebrew"),
                cellar=Path("/opt/homebrew/Cellar"),
                repository=Path("/opt/homebrew/Library/Homebrew"),
            )
