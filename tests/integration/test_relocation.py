"""Integration tests for the bottle relocation engine.

Relocation is fused into extraction, so almost everything here pours a real
tarball through `extract_bottle` with a `StreamRelocator` attached: the synthetic
bottles cover the stream's own logic, and the `cc`-built and brew-cached bottles
cover that a rewritten binary is still signed and loadable.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_extraction import make_tar

from brewery.core.config import get_brewery_env
from brewery.core.errors import RelocationError
from brewery.providers.bottle_config import install_etc_var
from brewery.providers.extractor import extract_bottle
from brewery.providers.relocator import RelocationResult, StreamRelocator
from brewery.providers.relocator import keg as keg_mod
from brewery.providers.relocator import macho as macho_mod
from brewery.providers.relocator import substitutions as subs_mod

_DARWIN = sys.platform == "darwin"
_LINUX = sys.platform.startswith("linux")
_HAS_CC = shutil.which("cc") is not None
_HAS_TOOLS = shutil.which("install_name_tool") and shutil.which("codesign")
_HAS_PATCHELF = shutil.which("patchelf") is not None
_HAS_BREW = shutil.which("brew") is not None
_FETCH = os.environ.get("BREWERY_FETCH") == "1"

requires_toolchain = pytest.mark.skipif(
    not (_DARWIN and _HAS_CC and _HAS_TOOLS),
    reason="requires macOS with cc, install_name_tool, codesign",
)

requires_elf_toolchain = pytest.mark.skipif(
    not (_LINUX and _HAS_CC and _HAS_PATCHELF),
    reason="requires Linux with cc and patchelf",
)

skip_no_brew = pytest.mark.skipif(
    not (_DARWIN and _HAS_BREW),
    reason="requires macOS with Homebrew installed",
)

# The fake prefix every synthetic pour relocates into
PREFIX = Path("/opt/hb")
CELLAR = PREFIX / "Cellar"
REPOSITORY = PREFIX / "Homebrew"

# Test list: many dylibs, an rpath/executable-heavy keg, a keg-only lib,
# and one typically marked :any_skip_relocation to exercise the no-op path
REAL_FORMULAE = ["openssl@3", "sqlite", "node", "zlib"]


def _brew(*args: str) -> str:
    """Run a Homebrew command and return the output.

    Args:
        *args: The arguments to pass to the `brew` command.

    Returns:
        The output of the `brew` command.
    """
    return subprocess.run(
        ["brew", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _sink(**kwargs) -> StreamRelocator:
    """Build a sink against the fixed fake prefix.

    Args:
        **kwargs: Overrides passed straight to StreamRelocator.

    Returns:
        The configured sink.
    """
    return StreamRelocator(
        prefix=PREFIX, cellar=CELLAR, repository=REPOSITORY, **kwargs
    )


def _pour(
    tmp_path: Path, entries: list[tuple], **kwargs
) -> tuple[Path, RelocationResult]:
    """Build a bottle from `entries` and pour it through the fused path.

    Args:
        tmp_path: The temporary directory to stage in.
        entries: The tar members, in `make_tar` form.
        **kwargs: Overrides passed straight to StreamRelocator.

    Returns:
        The keg directory and the RelocationResult.
    """
    bottle = tmp_path / "bottle.tar.gz"
    bottle.write_bytes(gzip.compress(make_tar(entries)))

    sink = _sink(**kwargs)
    keg = extract_bottle(bottle, tmp_path / "stage", sink=sink)

    return keg, sink.finish(keg)


def _keg(name: str = "foo", version: str = "1.0") -> str:
    """The staging-relative keg root a bottle unpacks to.

    Args:
        name: The formula name.
        version: The formula version.

    Returns:
        The `<name>/<version>` prefix every member sits under.
    """
    return f"{name}/{version}"


def _macho_files(root: Path) -> list[Path]:
    """Get a list of Mach-O files in a directory tree.

    Args:
        root: The root directory to search.

    Returns:
        A list of Mach-O files.
    """
    return [
        p
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and macho_mod.is_macho(p)
    ]


def _has_placeholder(root: Path) -> list[Path]:
    """Get a list of placeholder symlinks in a directory tree.

    Args:
        root: The root directory to search.

    Returns:
        A list of placeholder symlinks.
    """
    hits = []
    for p in root.rglob("*"):
        if p.is_symlink():
            if subs_mod._PLACEHOLDER_MARKER in os.readlink(p).encode():
                hits.append(p)

        elif p.is_file() and subs_mod._PLACEHOLDER_MARKER in p.read_bytes():
            hits.append(p)

    return hits


class TestText:
    """Tests for text members substituted in the stream."""

    def test_text_is_substituted_in_the_stream(self, tmp_path) -> None:
        """Test a text member is written with its final bytes once."""
        base = _keg()
        keg, result = _pour(
            tmp_path,
            [("file", f"{base}/bin/foo", b"#!@@HOMEBREW_PREFIX@@/bin/sh\n", 0o755)],
        )

        assert (keg / "bin/foo").read_bytes() == b"#!/opt/hb/bin/sh\n"
        assert result.changed_files == ["bin/foo"]

    def test_marker_free_text_is_untouched(self, tmp_path) -> None:
        """Test a file with no placeholder is neither rewritten nor reported."""
        base = _keg()
        keg, result = _pour(
            tmp_path, [("file", f"{base}/share/doc", b"nothing to see here\n", 0o644)]
        )

        assert (keg / "share/doc").read_bytes() == b"nothing to see here\n"
        assert result.changed_files == []

    def test_mode_survives_substitution(self, tmp_path) -> None:
        """Test a read-only text member keeps its mode after being rewritten."""
        base = _keg()
        keg, _ = _pour(
            tmp_path, [("file", f"{base}/etc/cfg", b"dir=@@HOMEBREW_CELLAR@@\n", 0o444)]
        )

        assert (keg / "etc/cfg").read_bytes() == b"dir=/opt/hb/Cellar\n"
        assert (keg / "etc/cfg").stat().st_mode & 0o777 == 0o444

    def test_oversized_text_falls_back_to_the_post_pass(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test a text file over the buffer cap is deferred, not skipped."""
        monkeypatch.setattr(keg_mod, "_STREAM_TEXT_CAP", 16)
        base = _keg()
        body = b"@@HOMEBREW_PREFIX@@/lib and a long tail past the cap\n"
        keg, result = _pour(tmp_path, [("file", f"{base}/etc/big", body, 0o644)])

        assert (keg / "etc/big").read_bytes().startswith(b"/opt/hb/lib")
        assert result.changed_files == ["etc/big"]

    def test_unresolved_placeholder_aborts_the_pour(self, tmp_path) -> None:
        """Test a token with no substitution raises rather than shipping.

        The error escapes from inside `extract_bottle`; it is a SysError, not an
        OSError, so the extraction wrapper does not swallow it.
        """
        base = _keg()
        with pytest.raises(RelocationError, match="unresolved placeholder"):
            _pour(tmp_path, [("file", f"{base}/bin/x", b"@@HOMEBREW_NOPE@@\n", 0o644)])


class TestLinks:
    """Tests for symlink targets substituted as the link is created."""

    def test_symlink_target_is_substituted_at_creation(self, tmp_path) -> None:
        """Test a placeholder symlink target is rewritten as the link is made."""
        base = _keg()
        keg, result = _pour(
            tmp_path,
            [("link", f"{base}/bin/foo", "@@HOMEBREW_PREFIX@@/opt/foo/bin/foo")],
        )

        assert os.readlink(keg / "bin/foo") == "/opt/hb/opt/foo/bin/foo"
        assert result.symlinks_relocated == 1

    def test_relative_symlink_is_left_alone(self, tmp_path) -> None:
        """Test a target with no placeholder is created verbatim."""
        base = _keg()
        keg, result = _pour(
            tmp_path,
            [
                ("file", f"{base}/lib/libfoo.1.dylib", b"x" * 32, 0o444),
                ("link", f"{base}/lib/libfoo.dylib", "libfoo.1.dylib"),
            ],
        )

        assert os.readlink(keg / "lib/libfoo.dylib") == "libfoo.1.dylib"
        assert result.symlinks_relocated == 0

    def test_bare_extraction_leaves_targets_verbatim(self, tmp_path) -> None:
        """Test `sink=None` is still today's behaviour exactly."""
        base = _keg()
        bottle = tmp_path / "bottle.tar.gz"
        bottle.write_bytes(
            gzip.compress(
                make_tar([("link", f"{base}/bin/foo", "@@HOMEBREW_PREFIX@@/bin/foo")])
            )
        )
        keg = extract_bottle(bottle, tmp_path / "stage")

        assert os.readlink(keg / "bin/foo") == "@@HOMEBREW_PREFIX@@/bin/foo"


class TestKegBoundary:
    """Tests for what the stream counts as part of the keg."""

    def test_dotbrew_is_never_relocated(self, tmp_path) -> None:
        """Test `<name>/.brew` stays verbatim and out of changed_files.

        `install_to_cellar` clones only the version directory, so the formula
        file brew ships alongside it is not part of the keg.
        """
        keg, result = _pour(
            tmp_path,
            [
                ("file", f"{_keg()}/bin/x", b"plain\n", 0o755),
                ("dir", "foo/.brew", 0o755),
                (
                    "file",
                    "foo/.brew/foo.rb",
                    b'prefix "@@HOMEBREW_PREFIX@@"\n',
                    0o644,
                ),
            ],
        )

        rb = keg.parent / ".brew" / "foo.rb"
        assert rb.read_bytes() == b'prefix "@@HOMEBREW_PREFIX@@"\n'
        assert result.changed_files == []

    def test_dotbottle_config_is_relocated_then_copied_into_the_prefix(
        self, tmp_path
    ) -> None:
        """Test bottled config is substituted in-stream and copied in as a real file.

        `.bottle/etc` sits inside the version directory, so it is part of the keg
        and its placeholders should be rewritten like any other listed text file.
        """
        base = _keg()
        keg, result = _pour(
            tmp_path,
            [
                ("file", f"{base}/bin/x", b"plain\n", 0o755),
                ("dir", f"{base}/.bottle/etc/foo", 0o755),
                (
                    "file",
                    f"{base}/.bottle/etc/foo/foo.conf",
                    b'root = "@@HOMEBREW_PREFIX@@/var/foo"\n',
                    0o644,
                ),
            ],
            text_files=[".bottle/etc/foo/foo.conf"],
        )

        prefix = tmp_path / "prefix"
        copied = install_etc_var(keg, prefix=prefix)

        conf = prefix / "etc" / "foo" / "foo.conf"
        assert result.changed_files == [".bottle/etc/foo/foo.conf"]
        assert conf.read_bytes() == f'root = "{PREFIX}/var/foo"\n'.encode()
        assert not conf.is_symlink()
        assert copied.copied == ["etc/foo/foo.conf"]

    def test_ar_archive_with_a_placeholder_is_rejected(self, tmp_path) -> None:
        """Test a static archive is deferred and still fails the marker check.

        Substituting one in the stream would move every header offset, so it is
        deferred to `finish` and `_process_file` raises there.
        """
        base = _keg()
        body = b"!<arch>\n" + b"padding @@HOMEBREW_PREFIX@@/lib padding"

        with pytest.raises(RelocationError, match="static archive"):
            _pour(tmp_path, [("file", f"{base}/lib/libfoo.a", body, 0o444)])


class TestManifest:
    """Tests for the tab's changed_files list gating the text branch."""

    def test_only_listed_files_are_substituted(self, tmp_path) -> None:
        """Test the tab's changed_files gates text substitution."""
        base = _keg()
        keg, result = _pour(
            tmp_path,
            [
                ("file", f"{base}/bin/listed", b"@@HOMEBREW_PREFIX@@/a\n", 0o755),
                ("file", f"{base}/bin/unlisted", b"@@HOMEBREW_PREFIX@@/b\n", 0o755),
            ],
            text_files=["bin/listed"],
        )

        assert (keg / "bin/listed").read_bytes() == b"/opt/hb/a\n"
        assert (keg / "bin/unlisted").read_bytes() == b"@@HOMEBREW_PREFIX@@/b\n"
        assert result.changed_files == ["bin/listed"]

    def test_missing_manifest_entry_raises_in_finish(self, tmp_path) -> None:
        """Test a listed file the tarball never carried aborts the pour.

        A stream has no finished tree to pre-check, so a manifest naming a file
        the bottle does not carry is caught in `finish` after the write, but
        before anything leaves staging.
        """
        base = _keg()
        with pytest.raises(RelocationError, match="manifest changed_files entry"):
            _pour(
                tmp_path,
                [("file", f"{base}/bin/x", b"plain\n", 0o755)],
                text_files=["bin/absent"],
            )

    def test_manifest_entry_naming_a_symlink_is_accepted(self, tmp_path) -> None:
        """Test a tab listing a symlink still installs.

        A tab may name a symlink, and brew tolerates it; in the stream a symlink
        is a deferred link member and is never seen as a regular file, so it has
        to be marked seen when the link is created or `finish` would reject it.
        """
        base = _keg()
        keg, result = _pour(
            tmp_path,
            [
                ("file", f"{base}/bin/real", b"plain\n", 0o755),
                ("link", f"{base}/bin/alias", "real"),
            ],
            text_files=["bin/alias"],
        )

        assert os.readlink(keg / "bin/alias") == "real"
        assert result.changed_files == ["bin/alias"]

    def test_manifest_entry_naming_a_hardlink_is_accepted(self, tmp_path) -> None:
        """Test a tab listing a hard link is seen, as `is_file()` would see it."""
        base = _keg()
        keg, _ = _pour(
            tmp_path,
            [
                ("file", f"{base}/bin/real", b"plain\n", 0o755),
                ("hardlink", f"{base}/bin/alias", f"{base}/bin/real"),
            ],
            text_files=["bin/alias"],
        )

        assert (keg / "bin/alias").read_bytes() == b"plain\n"


class TestSkipRelocation:
    """Tests for `:any_skip_relocation` bottles."""

    def test_text_still_runs_when_linkage_is_skipped(self, tmp_path) -> None:
        """Test `:any_skip_relocation` leaves binaries but not text alone."""
        base = _keg()
        keg, result = _pour(
            tmp_path,
            [("file", f"{base}/bin/foo", b"@@HOMEBREW_PREFIX@@/bin\n", 0o755)],
            skip_relocation=True,
        )

        assert (keg / "bin/foo").read_bytes() == b"/opt/hb/bin\n"
        assert result.changed_files == ["bin/foo"]
        assert result.macho_relocated == 0


@pytest.fixture(scope="module")
def dylib(tmp_path_factory) -> bytes:
    """Compile a dylib whose install name embeds a placeholder, mimicking a
    bottled library.

    Module-scoped: the bytes are poured into a fresh keg per test, so one
    compile serves them all.

    Args:
        tmp_path_factory: The session temp-dir factory.

    Returns:
        The compiled dylib's bytes.
    """
    build = tmp_path_factory.mktemp("dylib")
    src = build / "foo.c"
    src.write_text("int foo(void){return 42;}\n")
    lib = build / "libfoo.dylib"
    subprocess.run(
        [
            "cc",
            "-dynamiclib",
            "-o",
            str(lib),
            str(src),
            "-install_name",
            "@@HOMEBREW_PREFIX@@/lib/libfoo.dylib",
            "-Wl,-headerpad_max_install_names",
        ],
        check=True,
    )

    return lib.read_bytes()


@requires_toolchain
class TestMachO:
    """Tests for the Mach-O post-pass against a real cc-built dylib."""

    def test_install_name_is_rewritten_and_signature_valid(
        self, tmp_path, dylib
    ) -> None:
        """Test a Mach-O is deferred, patched into the target prefix, and signed."""
        base = _keg()
        keg, result = _pour(
            tmp_path, [("file", f"{base}/lib/libfoo.dylib", dylib, 0o755)]
        )
        lib = keg / "lib/libfoo.dylib"
        assert result.macho_relocated == 1

        names = macho_mod.find_install_names(lib)
        assert not any(subs_mod._PLACEHOLDER_MARKER_STR in n.value for n in names)

        # The install name now points into the target prefix
        out = subprocess.run(
            ["otool", "-D", str(lib)], capture_output=True, text=True, check=False
        ).stdout
        assert "/opt/hb/lib/libfoo.dylib" in out
        assert "@@HOMEBREW" not in out

        # Re-sign must leave a structurally valid signature
        verify = subprocess.run(
            ["codesign", "--verify", "--strict", str(lib)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert verify.returncode == 0, verify.stderr

    def test_loadable_after_relocation(self, tmp_path, dylib) -> None:
        """Test that pour, then dlopen to prove the binary actually runs (catches a missed
        re-sign, which presents as SIGKILL on arm64)."""
        import ctypes

        base = _keg()
        keg, _ = _pour(tmp_path, [("file", f"{base}/lib/libfoo.dylib", dylib, 0o755)])

        # Raises OSError if the loader rejects it
        lib = ctypes.CDLL(str(keg / "lib/libfoo.dylib"))
        lib.foo.restype = ctypes.c_int
        assert lib.foo() == 42

    def test_every_name_of_a_hardlinked_macho_is_signed(self, tmp_path, dylib) -> None:
        """Test a hard link's other name does not keep unsigned bytes.

        The inode is patched once, under the name that was deferred; `codesign`
        then replaces each file it signs rather than rewriting it, so a name
        left out of the batch would still point at the patched-but-unsigned inode.
        """
        base = _keg()
        keg, result = _pour(
            tmp_path,
            [
                ("file", f"{base}/bin/tool", dylib, 0o555),
                ("hardlink", f"{base}/bin/tool6", f"{base}/bin/tool"),
            ],
        )

        for rel in ("bin/tool", "bin/tool6"):
            path = keg / rel
            # Each name is signed under its own identity
            verify = subprocess.run(
                ["codesign", "--verify", str(path)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert verify.returncode == 0, f"{rel}: {verify.stderr.strip()}"

            names = macho_mod.find_install_names(path)
            assert not any(subs_mod._PLACEHOLDER_MARKER_STR in n.value for n in names)
            assert path.stat().st_mode & 0o777 == 0o555

        # One inode patched, under one name, however many names it carries
        assert result.macho_relocated == 1


@pytest.fixture(scope="module")
def elf_so(tmp_path_factory) -> bytes:
    """Compile a tiny ELF shared object with a placeholder RPATH, mimicking a
    Linux bottle library.

    Args:
        tmp_path_factory: The session temp-dir factory.

    Returns:
        The compiled shared object's bytes.
    """
    build = tmp_path_factory.mktemp("elf")
    src = build / "foo.c"
    src.write_text("int foo(void){return 42;}\n")
    lib = build / "libfoo.so"
    subprocess.run(
        [
            "cc",
            "-shared",
            "-fPIC",
            "-o",
            str(lib),
            str(src),
            "-Wl,-rpath,@@HOMEBREW_PREFIX@@/lib",
            "-Wl,--enable-new-dtags",  # emit DT_RUNPATH
        ],
        check=True,
    )

    return lib.read_bytes()


@requires_elf_toolchain
class TestElf:
    """Tests for the ELF post-pass against a real cc-built shared object."""

    def test_rewrites_rpath(self, tmp_path, elf_so) -> None:
        """Test that patchelf rewrites the RPATH into the target prefix."""
        base = _keg()
        keg, result = _pour(
            tmp_path, [("file", f"{base}/lib/libfoo.so", elf_so, 0o755)]
        )
        assert result.elf_relocated == 1
        assert result.macho_relocated == 0

        out = subprocess.run(
            ["patchelf", "--print-rpath", str(keg / "lib/libfoo.so")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "/opt/hb/lib" in out
        assert "@@HOMEBREW" not in out

    def test_loadable_after_relocation(self, tmp_path, elf_so) -> None:
        """Test that pour, then dlopen to prove the ELF still loads and runs."""
        import ctypes

        base = _keg()
        keg, _ = _pour(tmp_path, [("file", f"{base}/lib/libfoo.so", elf_so, 0o755)])

        # Raises OSError if the loader rejects it
        lib = ctypes.CDLL(str(keg / "lib/libfoo.so"))
        lib.foo.restype = ctypes.c_int
        assert lib.foo() == 42


@pytest.fixture(scope="module")
def brew_env() -> dict:
    """Run all brew introspection once for the module.

    Returns:
        Dict with keys `prefix`, `repository`, and `bottles`
        (mapping formula name → Path or None).
    """
    env = get_brewery_env()
    prefix = env.prefix
    repository = env.repository

    if _FETCH:
        subprocess.run(
            ["brew", "fetch", *REAL_FORMULAE],
            capture_output=True,
            text=True,
            check=False,
        )

    try:
        lines = _brew("--cache", *REAL_FORMULAE).splitlines()

    except subprocess.CalledProcessError:
        lines = []

    bottles: dict[str, Path | None] = {f: None for f in REAL_FORMULAE}
    for formula, line in zip(REAL_FORMULAE, lines):
        p = Path(line)
        bottles[formula] = p if p.exists() else None

    return {"prefix": prefix, "repository": repository, "bottles": bottles}


@pytest.fixture(scope="module", params=REAL_FORMULAE)
def relocated_real_keg(request, tmp_path_factory, brew_env) -> tuple[Path, Path]:
    """Yield (relocated_copy_keg, installed_keg) for a real formula, or skip.

    Module-scoped so each keg is extracted and relocated once and shared across
    the three read-only assertions below, rather than redone per test.

    Args:
        request: The pytest request object.
        tmp_path_factory: The session temp-dir factory.
        brew_env: Pre-computed brew prefix, repository, and bottle paths.

    Returns:
        A tuple containing the paths to the relocated copy keg and the installed keg.
    """
    formula = request.param
    prefix = brew_env["prefix"]

    bottle = brew_env["bottles"].get(formula)
    if bottle is None:
        pytest.skip(f"{formula} bottle not cached (set BREWERY_FETCH=1 to fetch)")

    sink = StreamRelocator(
        prefix=prefix,
        cellar=prefix / "Cellar",
        repository=brew_env["repository"],
        skip_relocation=False,
    )
    dest = tmp_path_factory.mktemp(formula.replace("@", "_"))

    # `sink.finish` needs the keg, so the version has to be known before the
    # staleness skip; extracting is what resolves it
    keg = extract_bottle(bottle, dest, sink=sink)
    installed = prefix / "Cellar" / formula / keg.name
    if not installed.is_dir():
        pytest.skip(f"{formula} {keg.name} not installed (cached bottle is stale)")

    sink.finish(keg)

    return keg, installed


@skip_no_brew
class TestRelocationRealKegs:
    """Test the relocation of real kegs."""

    def test_no_placeholders_remain(self, relocated_real_keg) -> None:
        """Test that no placeholders remain in the relocated keg."""
        keg, _ = relocated_real_keg
        leftover = _has_placeholder(keg)
        assert not leftover, f"Placeholders survived in: {leftover}"

    def test_all_macho_signatures_valid(self, relocated_real_keg) -> None:
        """Test that all Mach-O binaries have valid signatures."""
        keg, _ = relocated_real_keg
        for binary in _macho_files(keg):
            res = subprocess.run(
                ["codesign", "--verify", "--strict", str(binary)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0, f"{binary}: {res.stderr.strip()}"

    def test_install_names_match_installed_keg(self, relocated_real_keg) -> None:
        """Test that install names in the relocated keg match those in the installed keg."""
        keg, installed = relocated_real_keg
        mismatches: list[str] = []
        for binary in _macho_files(keg):
            rel = binary.relative_to(keg)
            ref = installed / rel
            if not ref.exists():
                continue  # Symlinked/version-specific path differences

            br = {(n.kind, n.value) for n in macho_mod.find_install_names(binary)}
            brew = {(n.kind, n.value) for n in macho_mod.find_install_names(ref)}
            if br != brew:
                mismatches.append(
                    f"{rel}\n  Only brewery's:   {sorted(br - brew)}"
                    f"\n  Only brew's: {sorted(brew - br)}"
                )

        # A diff here reveals any fixups brew performs beyond placeholder substitution
        assert not mismatches, "install-name divergence from brew:\n" + "\n".join(
            mismatches
        )
