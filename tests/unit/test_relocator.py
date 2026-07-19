"""Unit tests for the bottle relocation engine."""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

import pytest

from brewery.core import host
from brewery.core.host import Platform
from brewery.providers import relocator as r
from brewery.providers.receipt import RuntimeDependency
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
def subs(brew_paths) -> dict[bytes, bytes]:
    """Fixture for building substitution mappings.

    Returns:
        A dictionary mapping placeholder bytes to their resolved values.
    """
    return r.build_substitutions(**brew_paths)


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


def _dep(full_name: str, *, declared_directly: bool = True) -> RuntimeDependency:
    """Build a runtime dependency entry.

    Args:
        full_name: The dependency's full name.
        declared_directly: Whether the formula declares the dep itself.

    Returns:
        The RuntimeDependency entry.
    """
    return RuntimeDependency(
        full_name=full_name, version="1", declared_directly=declared_directly
    )


@pytest.fixture
def system_perl(monkeypatch):
    """Pretend only /usr/bin/perl5.34 is present on the host.

    Keeps the perl-path resolution off the real filesystem, which otherwise
    varies by macOS version.

    Args:
        monkeypatch: The monkeypatch fixture.
    """
    monkeypatch.setattr(
        r.Path, "exists", lambda self: str(self) == "/usr/bin/perl5.34", raising=False
    )


class TestFormulaTokens:
    """Tests for the per-formula token map (brew's prepare_relocation_to_locations)."""

    def test_perl_from_system_when_uses_from_macos(
        self, brew_paths, system_perl
    ) -> None:
        """The cloc case: perl is uses_from_macos, so it is not a dep on macOS.

        The shebang must point at the system perl the bottle was built against,
        not at an opt/perl that was never installed.
        """
        tokens = r.formula_tokens(
            brew_paths["prefix"],
            name="cloc",
            runtime_deps=[],
            built_on={"preferred_perl": "5.34"},
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/usr/bin/perl5.34"

    def test_perl_from_brewed_perl_when_declared(self, brew_paths, system_perl) -> None:
        """Tests that a formula declaring perl gets the opt-linked brewed perl."""
        tokens = r.formula_tokens(
            brew_paths["prefix"],
            name="ack",
            runtime_deps=[_dep("perl")],
            built_on={"preferred_perl": "5.34"},
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/opt/homebrew/opt/perl/bin/perl"

    def test_perl_itself_gets_brewed_perl(self, brew_paths, system_perl) -> None:
        """Tests that the perl formula resolves to its own opt path."""
        tokens = r.formula_tokens(brew_paths["prefix"], name="perl", runtime_deps=[])
        assert tokens["@@HOMEBREW_PERL@@"] == "/opt/homebrew/opt/perl/bin/perl"

    def test_indirect_perl_dep_uses_system_perl(self, brew_paths, system_perl) -> None:
        """A perl pulled in transitively is not a declared dep, so brew uses system perl."""
        tokens = r.formula_tokens(
            brew_paths["prefix"],
            name="cloc",
            runtime_deps=[_dep("perl", declared_directly=False)],
            built_on={"preferred_perl": "5.34"},
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/usr/bin/perl5.34"

    @pytest.mark.parametrize(
        "built_on",
        [None, {}, {"preferred_perl": "5.99"}, {"preferred_perl": "not-a-version"}],
        ids=["no-tab", "empty", "absent-from-host", "malformed"],
    )
    def test_perl_falls_back_to_host_preferred(
        self, brew_paths, system_perl, monkeypatch, built_on
    ) -> None:
        """Tests that an unusable tab value falls back to this host's preferred perl."""
        monkeypatch.setattr(
            host, "current_platform", lambda: Platform(arch="arm64", macos_major=12)
        )
        tokens = r.formula_tokens(
            brew_paths["prefix"], name="cloc", runtime_deps=[], built_on=built_on
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/usr/bin/perl5.30"

    def test_java_omitted_without_openjdk_dep(self, brew_paths, system_perl) -> None:
        """Tests that java is left undefined when no openjdk dep exists.

        brew leaves @@HOMEBREW_JAVA@@ alone in that case, so we must too.
        """
        tokens = r.formula_tokens(
            brew_paths["prefix"], name="cloc", runtime_deps=[_dep("zlib")]
        )
        assert "@@HOMEBREW_JAVA@@" not in tokens

    @pytest.mark.parametrize("dep", ["openjdk", "openjdk@21", "openjdk@11.0"])
    def test_java_resolved_from_openjdk_dep(self, brew_paths, system_perl, dep) -> None:
        """Tests that java resolves to JAVA_HOME inside the openjdk dep's bundle.

        On macOS that is libexec/openjdk.jdk/Contents/Home, not libexec itself:
        a launcher pointed at libexec finds no bin/java.
        """
        tokens = r.formula_tokens(
            brew_paths["prefix"], name="jenkins", runtime_deps=[_dep(dep)]
        )
        assert tokens["@@HOMEBREW_JAVA@@"] == (
            f"/opt/homebrew/opt/{dep}/libexec/openjdk.jdk/Contents/Home"
        )

    def test_openjdk_lookalike_dep_ignored(self, brew_paths, system_perl) -> None:
        """Tests that a dep merely containing 'openjdk' does not resolve java."""
        tokens = r.formula_tokens(
            brew_paths["prefix"],
            name="jenkins",
            runtime_deps=[_dep("openjdk-headless")],
        )
        assert "@@HOMEBREW_JAVA@@" not in tokens


@pytest.fixture
def mock_run(monkeypatch):
    """Patch the relocator's subprocess boundary with a recording stub.

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
        """Install the stub and return the call-log list.

        Args:
            stdout: stdout text the stub returns in CompletedProcess.
            stderr: stderr text the stub returns in CompletedProcess.
            returncode: The return code the stub reports.

        Returns:
            A list that accumulates one argv list per subprocess.run call.
        """
        runs: list[list[str]] = []

        def stub(cmd, *args, **kwargs) -> subprocess.CompletedProcess:
            """Record the command and return a CompletedProcess stub.

            Args:
                cmd: The command to record and return.
                *args: Additional args to pass to subprocess.run.
                **kwargs: Additional kwargs to pass to subprocess.run.

            Returns:
                A CompletedProcess stub with the given return code and stdout/stderr.
            """
            runs.append(list(cmd))

            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        monkeypatch.setattr(r.subprocess, "run", stub)

        return runs

    return install


def _keg_with_dylib(root: Path, load_commands: list[bytes], name: str = "libfoo.dylib"):
    """Write a single Mach-O into a keg's lib/ dir and return (keg, dylib path).

    Args:
        root: The temp dir to build the keg under.
        load_commands: The load commands for the fake thin Mach-O.
        name: The dylib filename.

    Returns:
        A (keg_dir, dylib_path) tuple.
    """
    keg = root / "keg"
    (keg / "lib").mkdir(parents=True)
    dylib = keg / "lib" / name
    dylib.write_bytes(_thin_macho(load_commands))

    return keg, dylib


class TestMachORewrite:
    """Tests for Mach-O relocation, driven through the real keg walker."""

    def test_relocate_keg_builds_correct_macho_argv(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Tests that the correct arguments are passed to the relocation commands."""
        runs = mock_run()
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [
                _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
                _lc_dylib(r._LC_LOAD_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libbar.dylib"),
                _lc_dylib(r._LC_LOAD_DYLIB, "/usr/lib/libSystem.B.dylib"),  # untouched
                _lc_rpath("@@HOMEBREW_CELLAR@@/foo/1.0/lib"),
            ],
        )

        result = r.relocate_keg(keg, **brew_paths)
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

    def test_relocate_keg_noop_when_no_placeholders(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """Tests that no changes are made when there are no placeholders."""
        runs = mock_run()
        keg, _ = _keg_with_dylib(
            tmp_path, [_lc_dylib(r._LC_ID_DYLIB, "/usr/lib/libSystem.B.dylib")]
        )

        # No placeholders -> no rewrite, and no re-sign
        result = r.relocate_keg(keg, **brew_paths)
        assert result.macho_relocated == 0
        assert runs == []

    def test_install_name_tool_failure_raises_relocation_error(
        self, tmp_path, brew_paths, mock_run
    ) -> None:
        """A failing install_name_tool aborts with the offending file and reason."""
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [_lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib")],
        )

        mock_run(stderr="load command too large", returncode=1)
        with pytest.raises(RelocationError) as exc:
            r.relocate_keg(keg, **brew_paths)
        assert exc.value.path == dylib
        assert "too large" in exc.value.reason

    def test_relocate_macho_handles_readonly_binary(
        self, tmp_path, brew_paths, monkeypatch
    ) -> None:
        """Tests that Mach-O relocation handles read-only binaries correctly."""
        keg, dylib = _keg_with_dylib(
            tmp_path,
            [_lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libro.dylib")],
            name="libro.dylib",
        )
        os.chmod(dylib, 0o555)  # Typical read-only executable mode in a keg

        # Both install_name_tool and codesign must see a writable file
        seen_mode: list[int] = []

        def record(cmd) -> None:
            """Records the file's write bit at the moment _run is invoked.

            Args:
                cmd: The command that would have been run.
            """
            seen_mode.append(dylib.stat().st_mode & 0o200)

        monkeypatch.setattr(r, "_run", record)
        result = r.relocate_keg(keg, **brew_paths)
        assert result.macho_relocated == 1

        assert seen_mode and all(seen_mode), "file was not writable during the rewrite"
        assert oct(dylib.stat().st_mode & 0o777) == "0o555"  # Mode restored


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
                    _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
                ]
            )
        )

        # Symlink with placeholder target
        os.symlink("@@HOMEBREW_PREFIX@@/bin/foo", keg / "bin" / "foo")

        # Untouched file
        (keg / "lib" / "data.txt").write_text("no tokens\n")

        mock_run()

        result = r.relocate_keg(keg, **brew_paths)

        # Fallback scan: text file + macho + symlink modified, data.txt untouched
        assert result.changed_files == ["bin/foo-config"]
        assert result.macho_relocated == 1
        assert result.symlinks_relocated == 1
        assert "@@HOMEBREW" not in (keg / "bin" / "foo-config").read_text()
        assert os.readlink(keg / "bin" / "foo") == "/opt/homebrew/bin/foo"

    def test_relocate_keg_uses_manifest_text_files_and_skips_scan(
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
        # macho + symlink still handled by the walk
        (keg / "lib" / "libfoo.dylib").write_bytes(
            _thin_macho(
                [
                    _lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib"),
                ]
            )
        )
        os.symlink("@@HOMEBREW_PREFIX@@/bin/foo", keg / "bin" / "foo")

        monkeypatch.setattr(r, "_run", lambda cmd: None)

        result = r.relocate_keg(keg, **brew_paths, text_files=["lib/pkgconfig/foo.pc"])
        assert result.changed_files == ["lib/pkgconfig/foo.pc"]
        assert result.macho_relocated == 1 and result.symlinks_relocated == 1
        assert "@@HOMEBREW" not in (keg / "lib" / "pkgconfig" / "foo.pc").read_text()

        # The unlisted text file was never read/substituted
        assert (keg / "bin" / "stray").read_text() == "p=@@HOMEBREW_PREFIX@@\n"

    def test_relocate_keg_raises_when_listed_text_file_missing(
        self, tmp_path, monkeypatch, brew_paths
    ) -> None:
        """Tests that a missing listed text file raises an error."""
        keg = tmp_path / "keg"
        keg.mkdir()
        monkeypatch.setattr(r, "_run", lambda cmd: None)
        with pytest.raises(RelocationError, match="missing from keg"):
            r.relocate_keg(keg, **brew_paths, text_files=["lib/pkgconfig/gone.pc"])

    def test_relocate_keg_skip_relocation_still_substitutes_text(
        self, tmp_path, mock_run, brew_paths
    ) -> None:
        """skip_relocation maps to brew's skip_linkage: Mach-O install names are
        left alone, but text placeholders are still substituted (a script that
        sources @@HOMEBREW_CELLAR@@/... would break at runtime otherwise)."""
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        (keg / "config").write_text("p=@@HOMEBREW_PREFIX@@\n")
        (keg / "lib" / "libx.dylib").write_bytes(
            _thin_macho(
                [_lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libx.dylib")]
            )
        )

        called = mock_run()

        n = r.relocate_keg(keg, **brew_paths, skip_relocation=True)

        # Text file substituted; Mach-O linkage skipped (no install_name_tool)
        assert "@@HOMEBREW_PREFIX@@" not in (keg / "config").read_text()
        assert n.macho_relocated == 0
        assert called == []

    def test_relocate_keg_substitutes_formula_tokens(
        self, tmp_path, brew_paths, system_perl
    ) -> None:
        """A perl shebang is rewritten once the formula tokens are supplied."""
        keg = tmp_path / "keg"
        (keg / "libexec" / "bin").mkdir(parents=True)
        script = keg / "libexec" / "bin" / "cloc"
        script.write_text("#!@@HOMEBREW_PERL@@\nprint 1;\n")

        tokens = r.formula_tokens(
            brew_paths["prefix"],
            name="cloc",
            runtime_deps=[],
            built_on={"preferred_perl": "5.34"},
        )
        result = r.relocate_keg(keg, **brew_paths, extra_tokens=tokens)

        assert result.changed_files == ["libexec/bin/cloc"]
        assert script.read_text().startswith("#!/usr/bin/perl5.34\n")

    def test_relocate_keg_raises_on_unresolved_placeholder(
        self, tmp_path, brew_paths
    ) -> None:
        """A placeholder with no token in the map must abort, not ship broken.

        The regression this guards: @@HOMEBREW_PERL@@ was silently left in
        cloc's libexec shebang because the pipeline never passed extra_tokens.
        """
        keg = tmp_path / "keg"
        (keg / "libexec" / "bin").mkdir(parents=True)
        (keg / "libexec" / "bin" / "cloc").write_text("#!@@HOMEBREW_PERL@@\n")

        with pytest.raises(RelocationError, match=r"unresolved placeholder .*PERL"):
            r.relocate_keg(keg, **brew_paths)

    def test_relocate_keg_propagates_macho_failure(
        self, tmp_path, mock_run, brew_paths
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

        mock_run(stderr="load command too large", returncode=1)
        with pytest.raises(RelocationError):
            r.relocate_keg(keg, **brew_paths)

    def test_relocate_keg_batches_codesign_across_machos(
        self, tmp_path, mock_run, brew_paths
    ) -> None:
        """All rewritten Mach-O files are re-signed in one codesign call, after
        every install_name_tool has run."""
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        names = ["liba.dylib", "libb.dylib", "libc.dylib"]
        for name in names:
            (keg / "lib" / name).write_bytes(
                _thin_macho(
                    [_lc_dylib(r._LC_ID_DYLIB, f"@@HOMEBREW_PREFIX@@/lib/{name}")]
                )
            )

        runs = mock_run()

        result = r.relocate_keg(keg, **brew_paths)
        assert result.macho_relocated == 3

        int_runs = [cmd for cmd in runs if cmd[0] == "install_name_tool"]
        sign_runs = [cmd for cmd in runs if cmd[0] == "codesign"]
        assert len(int_runs) == 3  # one per binary
        assert len(sign_runs) == 1  # a single batched re-sign

        signed = {arg for arg in sign_runs[0] if arg.endswith(".dylib")}
        assert signed == {str(keg / "lib" / name) for name in names}

        # codesign must follow every install_name_tool (it strips the signature)
        assert runs.index(sign_runs[0]) == len(runs) - 1

    def test_relocate_keg_propagates_codesign_failure(
        self, tmp_path, monkeypatch, brew_paths
    ) -> None:
        """A failing batched codesign aborts the keg with a RelocationError even
        when install_name_tool succeeded."""
        keg = tmp_path / "keg"
        (keg / "lib").mkdir(parents=True)
        (keg / "lib" / "libx.dylib").write_bytes(
            _thin_macho(
                [_lc_dylib(r._LC_ID_DYLIB, "@@HOMEBREW_PREFIX@@/lib/libx.dylib")]
            )
        )

        def stub(cmd, *args, **kwargs) -> subprocess.CompletedProcess:
            failed = cmd[0] == "codesign"
            return subprocess.CompletedProcess(
                cmd, 1 if failed else 0, "", "bad signature" if failed else ""
            )

        monkeypatch.setattr(r.subprocess, "run", stub)

        with pytest.raises(RelocationError, match="codesign failed"):
            r.relocate_keg(keg, **brew_paths)


class TestChunkPaths:
    """Tests for the codesign argv chunker."""

    def test_single_chunk_under_budget(self) -> None:
        """A handful of short paths stay in one chunk."""
        paths = [Path(f"/keg/lib/lib{i}.dylib") for i in range(5)]
        assert r._chunk_paths(paths, budget=1024) == [paths]

    def test_splits_when_byte_budget_exceeded(self) -> None:
        """Paths are split into multiple chunks once the byte budget is hit."""
        paths = [Path("/keg/lib/" + "x" * 40 + f"{i}.dylib") for i in range(10)]
        chunks = r._chunk_paths(paths, budget=100)

        assert len(chunks) > 1
        # Every input path appears exactly once, order preserved
        assert [p for chunk in chunks for p in chunk] == paths
        # No chunk (beyond a lone oversized path) exceeds the budget
        for chunk in chunks:
            if len(chunk) > 1:
                assert sum(len(str(p).encode()) + 1 for p in chunk) <= 100

    def test_oversized_single_path_gets_own_chunk(self) -> None:
        """A path longer than the budget still yields exactly one chunk."""
        paths = [Path("/keg/" + "y" * 500 + ".dylib")]
        assert r._chunk_paths(paths, budget=100) == [paths]

    def test_empty_input(self) -> None:
        """No paths means no chunks."""
        assert r._chunk_paths([], budget=100) == []
