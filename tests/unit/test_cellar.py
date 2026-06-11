"""Unit tests for the Cellar installer.

The copytree path runs everywhere and covers clone fidelity, the opt symlink,
reinstall/upgrade behaviour, the clonefile->copytree fallback (simulated by
monkeypatching the syscall wrapper), and partial-keg cleanup on failure. The
native clonefile branch can only be exercised on macOS, so it lives in an
`integration` test that asserts clonefile and copytree produce identical
trees.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

import brewery.providers.cellar as _cellar
from brewery.providers.cellar import CellarError, clone_tree, install_to_cellar

pytestmark = pytest.mark.unit


def _build_keg(version_dir: Path) -> Path:
    """Build a keg directory structure for the specified version.

    Args:
        version_dir (Path): The path to the version directory.

    Returns:
        Path: The path to the created keg directory.
    """
    (version_dir / "bin").mkdir(parents=True)
    (version_dir / "lib").mkdir()

    exe = version_dir / "bin" / "openssl"
    exe.write_bytes(b"MACHO-binary")
    os.chmod(exe, 0o555)

    lib = version_dir / "lib" / "libssl.dylib"
    lib.write_bytes(b"lib")
    os.chmod(lib, 0o444)

    os.symlink("libssl.dylib", version_dir / "lib" / "libssl.3.dylib")
    (version_dir / ".brew").mkdir()
    (version_dir / ".brew" / "openssl@3.rb").write_bytes(b"class Openssl3\nend\n")

    return version_dir


@pytest.fixture
def staged_keg(tmp_path) -> Path:
    """Create a staged keg directory structure for testing.

    Args:
        tmp_path (Path): The temporary path fixture.

    Returns:
        Path: The path to the created staged keg directory.
    """
    return _build_keg(tmp_path / "stage" / "openssl@3" / "3.0")


@pytest.fixture
def prefix(tmp_path) -> Path:
    """Create a prefix directory structure for testing.

    Args:
        tmp_path (Path): The temporary path fixture.

    Returns:
        Path: The path to the created prefix directory.
    """
    return tmp_path / "prefix"


def _install(staged, prefix, name="openssl@3", version="3.0", **kw):
    """Install a keg into the Cellar.

    Args:
        staged (Path): The path to the staged keg.
        prefix (Path): The prefix path.
        name (str): The name of the formula.
        version (str): The version of the formula.
        **kw: Additional keyword arguments.

    Returns:
        Path: The path to the installed keg.
    """
    kw.setdefault("use_clonefile", False)

    return install_to_cellar(staged, prefix=prefix, name=name, version=version, **kw)


def test_clone_tree_preserves_modes_and_symlinks(staged_keg, tmp_path) -> None:
    """Test that cloning a directory preserves file modes and symlinks."""
    dst = tmp_path / "out"
    clone_tree(staged_keg, dst, use_clonefile=False)
    assert (dst / "bin" / "openssl").read_bytes() == b"MACHO-binary"
    assert oct((dst / "bin" / "openssl").stat().st_mode & 0o777) == "0o555"
    assert oct((dst / "lib" / "libssl.dylib").stat().st_mode & 0o777) == "0o444"
    assert os.readlink(dst / "lib" / "libssl.3.dylib") == "libssl.dylib"
    assert (dst / ".brew" / "openssl@3.rb").exists()


def test_clone_tree_refuses_existing_dest(staged_keg, tmp_path) -> None:
    """Test that cloning a directory refuses to overwrite an existing destination."""
    dst = tmp_path / "out"
    dst.mkdir()
    with pytest.raises(FileExistsError):
        clone_tree(staged_keg, dst, use_clonefile=False)


def test_clone_tree_falls_back_when_clonefile_unsupported(
    staged_keg, tmp_path, monkeypatch
) -> None:
    """Test that cloning a directory falls back when clonefile is unsupported."""

    def enotsup(src, dst) -> None:
        """Simulate ENOTSUP error."""
        raise OSError(errno.ENOTSUP, "not supported")

    monkeypatch.setattr(_cellar, "_clonefile", enotsup)
    dst = tmp_path / "out"
    clone_tree(staged_keg, dst, use_clonefile=True)  # Forced clonefile -> falls back
    assert (dst / "bin" / "openssl").read_bytes() == b"MACHO-binary"


def test_clone_tree_reraises_real_clonefile_error(
    staged_keg, tmp_path, monkeypatch
) -> None:
    """Test that cloning a directory reraises real clonefile errors."""

    def eacces(src, dst) -> None:
        """Simulate EACCES error."""
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(_cellar, "_clonefile", eacces)
    with pytest.raises(OSError) as exc:
        clone_tree(staged_keg, tmp_path / "out", use_clonefile=True)
    assert exc.value.errno == errno.EACCES  # Not swallowed as a fallback


def test_install_places_keg_and_opt_link(staged_keg, prefix) -> None:
    """Test that installing a keg places it in the correct location and creates a symlink in opt."""
    dest = _install(staged_keg, prefix)
    assert dest == prefix / "Cellar" / "openssl@3" / "3.0"
    assert (dest / "bin" / "openssl").read_bytes() == b"MACHO-binary"

    opt = prefix / "opt" / "openssl@3"
    assert opt.is_symlink()
    assert os.readlink(opt) == "../Cellar/openssl@3/3.0"  # Relative
    assert opt.resolve() == dest.resolve()


def test_reinstall_replaces_readonly_keg(staged_keg, prefix) -> None:
    """Test that reinstalling a keg replaces a readonly keg."""
    _install(staged_keg, prefix)

    # Mutate the staged source, reinstall the same version, expect replacement
    (staged_keg / "bin" / "openssl").chmod(0o755)
    (staged_keg / "bin" / "openssl").write_bytes(b"REBUILT")
    (staged_keg / "bin" / "openssl").chmod(0o555)
    dest = _install(staged_keg, prefix)
    assert (dest / "bin" / "openssl").read_bytes() == b"REBUILT"


def test_upgrade_repoints_opt_and_keeps_old_keg(staged_keg, prefix, tmp_path) -> None:
    """Test that upgrading a keg repoints the opt symlink and keeps the old keg."""
    _install(staged_keg, prefix, version="3.0")
    new = _build_keg(tmp_path / "stage2" / "openssl@3" / "3.1")
    (new / "bin" / "openssl").chmod(0o755)
    (new / "bin" / "openssl").write_bytes(b"v3.1")
    (new / "bin" / "openssl").chmod(0o555)

    _install(new, prefix, version="3.1")
    opt = prefix / "opt" / "openssl@3"
    assert os.readlink(opt) == "../Cellar/openssl@3/3.1"
    assert (prefix / "Cellar" / "openssl@3" / "3.0").exists()  # Old keg retained
    assert (
        prefix / "Cellar" / "openssl@3" / "3.1" / "bin" / "openssl"
    ).read_bytes() == b"v3.1"


def test_opt_refreshed_when_previously_dangling(staged_keg, prefix) -> None:
    """Test that the opt symlink is refreshed when it was previously dangling."""
    opt = prefix / "opt" / "openssl@3"
    opt.parent.mkdir(parents=True)
    opt.symlink_to(Path("..") / "Cellar" / "openssl@3" / "9.9")  # Points at nothing
    _install(staged_keg, prefix)
    assert os.readlink(opt) == "../Cellar/openssl@3/3.0"


def test_install_cleans_partial_keg_on_failure(staged_keg, prefix, monkeypatch) -> None:
    """Test that installing a keg cleans up partial installations on failure."""

    def half_then_fail(src, dst, *, use_clonefile=None) -> None:
        """Simulate a partial installation failure.

        Args:
            src: The source path.
            dst: The destination path.
            use_clonefile: Whether to use clonefile.
        """
        dst.mkdir(parents=True)
        (dst / "partial").write_bytes(b"x")
        raise OSError(errno.EIO, "disk error")

    monkeypatch.setattr(_cellar, "clone_tree", half_then_fail)
    with pytest.raises(CellarError):
        _install(staged_keg, prefix)
    assert not (prefix / "Cellar" / "openssl@3" / "3.0").exists()


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "darwin", reason="clonefile is macOS-only")
def test_clonefile_matches_copytree(staged_keg, tmp_path) -> None:
    """Test that clonefile and copytree produce the same result."""
    via_clone = tmp_path / "clone"
    via_copy = tmp_path / "copy"
    clone_tree(staged_keg, via_clone, use_clonefile=True)
    clone_tree(staged_keg, via_copy, use_clonefile=False)

    def snapshot(root: Path):
        out = {}
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if p.is_symlink():
                out[str(rel)] = ("link", os.readlink(p))

            elif p.is_file():
                out[str(rel)] = ("file", p.stat().st_mode & 0o777, p.read_bytes())

            else:
                out[str(rel)] = ("dir", p.stat().st_mode & 0o777)

        return out

    assert snapshot(via_clone) == snapshot(via_copy)
