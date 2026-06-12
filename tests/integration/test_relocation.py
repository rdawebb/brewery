"""Integration tests for the bottle relocation engine."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from brewery.core.config import get_brewery_env
from brewery.providers import relocator as r
from brewery.providers.extractor import extract_bottle

pytestmark = pytest.mark.integration

_DARWIN = sys.platform == "darwin"
_HAS_CC = shutil.which("cc") is not None
_HAS_TOOLS = shutil.which("install_name_tool") and shutil.which("codesign")
_HAS_BREW = shutil.which("brew") is not None
_FETCH = os.environ.get("BREWERY_FETCH") == "1"

requires_toolchain = pytest.mark.skipif(
    not (_DARWIN and _HAS_CC and _HAS_TOOLS),
    reason="requires macOS with cc, install_name_tool, codesign",
)

skip_no_brew = pytest.mark.skipif(
    not (_DARWIN and _HAS_BREW),
    reason="requires macOS with Homebrew installed",
)

# Test list: many dylibs, an rpath/executable-heavy keg, a keg-only lib,
# and one typically marked :any_skip_relocation to exercise the no-op path.
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


def _installed_keg(formula: str, prefix: Path) -> Path | None:
    """Get the installed keg path for a formula.

    Args:
        formula: The formula to check.
        prefix: The Homebrew prefix path.

    Returns:
        The path to the installed keg, or None if not found.
    """
    cellar = prefix / "Cellar" / formula
    if not cellar.is_dir():
        return None

    versions = sorted(p for p in cellar.iterdir() if p.is_dir())

    return versions[-1] if versions else None


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
        if p.is_file() and not p.is_symlink() and r.is_macho(p)
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
            if r._PLACEHOLDER_MARKER in os.readlink(p).encode():
                hits.append(p)

        elif p.is_file() and r._PLACEHOLDER_MARKER in p.read_bytes():
            hits.append(p)

    return hits


@pytest.fixture
def real_dylib(tmp_path) -> Path:
    """Compile a tiny dylib whose install name embeds a placeholder, mimicking
    a bottled library.

    Args:
        tmp_path: The temporary directory to use for the dylib.

    Returns:
        The path to the compiled dylib.
    """
    src = tmp_path / "foo.c"
    src.write_text("int foo(void){return 42;}\n")
    lib = tmp_path / "libfoo.dylib"
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

    return lib


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
            ["brew", "fetch", *REAL_FORMULAE], capture_output=True, text=True
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

    installed = _installed_keg(formula, prefix)
    if installed is None:
        pytest.skip(f"{formula} not installed")

    bottle = brew_env["bottles"].get(formula)
    if bottle is None:
        pytest.skip(f"{formula} bottle not cached (set BREWERY_FETCH=1 to fetch)")

    keg = extract_bottle(bottle, tmp_path_factory.mktemp(formula.replace("@", "_")))

    r.relocate_keg(
        keg,
        prefix=prefix,
        cellar=prefix / "Cellar",
        repository=brew_env["repository"],
        skip_relocation=False,
    )

    return keg, installed


@requires_toolchain
class TestRelocatorIntegration:
    """Test the integration of the relocator with real binaries."""

    def test_integration_relocates_and_signature_valid(
        self, real_dylib, tmp_path
    ) -> None:
        """Test that the relocator correctly relocates binaries and maintains valid signatures."""
        subs = r.build_substitutions(
            prefix=tmp_path,
            cellar=tmp_path / "Cellar",
            repository=tmp_path / "Library",
        )
        assert r.relocate_macho(real_dylib, subs) is True

        # The install name now points into the target prefix
        out = subprocess.run(
            ["otool", "-D", str(real_dylib)], capture_output=True, text=True
        ).stdout
        assert f"{tmp_path}/lib/libfoo.dylib" in out
        assert "@@HOMEBREW" not in out

        # Re-sign must leave a structurally valid signature
        verify = subprocess.run(
            ["codesign", "--verify", "--strict", str(real_dylib)],
            capture_output=True,
            text=True,
        )
        assert verify.returncode == 0, verify.stderr

    def test_integration_loadable_after_relocation(self, real_dylib, tmp_path) -> None:
        """Relocate, then dlopen via a tiny executable to prove the binary actually
        runs (catches a missed re-sign, which presents as SIGKILL on arm64)."""
        import ctypes

        subs = r.build_substitutions(
            prefix=tmp_path,
            cellar=tmp_path / "Cellar",
            repository=tmp_path / "Library",
        )
        r.relocate_macho(real_dylib, subs)
        lib = ctypes.CDLL(str(real_dylib))  # Raises OSError if the loader rejects it
        lib.foo.restype = ctypes.c_int
        assert lib.foo() == 42


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

            ours = {(n.kind, n.value) for n in r.find_install_names(binary)}
            theirs = {(n.kind, n.value) for n in r.find_install_names(ref)}
            if ours != theirs:
                mismatches.append(
                    f"{rel}\n  Only brewery's:   {sorted(ours - theirs)}"
                    f"\n  Only brew's: {sorted(theirs - ours)}"
                )

        # A diff here reveals any fixups brew performs beyond placeholder substitution
        assert not mismatches, "install-name divergence from brew:\n" + "\n".join(
            mismatches
        )
