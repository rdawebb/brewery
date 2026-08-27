"""Unit tests for the Cellar installer."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from _locks import held_rack

import brewery.providers.cellar as _cellar
from brewery.providers.cellar import (
    CellarError,
    clone_tree,
    install_to_cellar,
    remove_rack,
)


@pytest.fixture
def prefix_path(tmp_path) -> Path:
    """Create a prefix directory structure for testing.

    Args:
        tmp_path: The temporary path fixture.

    Returns:
        Path: The path to the created prefix directory.
    """
    return tmp_path / "prefix"


def _install(staged, prefix_path, name="openssl@3", version="3.0", **kw):
    """Install a keg into the Cellar.

    Args:
        staged: The path to the staged keg.
        prefix_path: The prefix path.
        name: The name of the formula.
        version: The version of the formula.
        **kw: Additional keyword arguments.

    Returns:
        The path to the installed keg.
    """
    kw.setdefault("use_clonefile", False)

    return install_to_cellar(
        staged, prefix=prefix_path, name=name, version=version, **kw
    )


@pytest.fixture
def clonefile_fails(monkeypatch):
    """Make the clonefile syscall fail with a given errno.

    Args:
        monkeypatch: The monkeypatch fixture.

    Returns:
        A callable taking the errno the syscall should raise.
    """

    def _fail(code: int) -> None:
        def _raise(src, dst) -> None:
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(_cellar, "_clonefile", _raise)

    return _fail


class TestCloneTree:
    """Tests the tree copier, including the clonefile fast path and its fallback."""

    def test_modes_and_symlinks_are_preserved(self, staged_keg, tmp_path) -> None:
        """Test that a copied keg keeps its permissions and relative symlinks."""
        dst = tmp_path / "out"
        clone_tree(staged_keg, dst, use_clonefile=False)
        assert (dst / "bin" / "openssl").read_bytes() == b"MACHO-binary"
        assert oct((dst / "bin" / "openssl").stat().st_mode & 0o777) == "0o555"
        assert oct((dst / "lib" / "libssl.dylib").stat().st_mode & 0o777) == "0o444"
        assert os.readlink(dst / "lib" / "libssl.3.dylib") == "libssl.dylib"
        assert (dst / ".brew" / "openssl@3.rb").exists()

    def test_an_existing_destination_is_refused(self, staged_keg, tmp_path) -> None:
        """Test that cloning never writes into a directory that is already there."""
        dst = tmp_path / "out"
        dst.mkdir()

        with pytest.raises(FileExistsError):
            clone_tree(staged_keg, dst, use_clonefile=False)

    def test_an_unsupported_filesystem_falls_back(
        self, staged_keg, tmp_path, clonefile_fails
    ) -> None:
        """Test that a filesystem without clonefile still gets its copy."""
        clonefile_fails(errno.ENOTSUP)
        dst = tmp_path / "out"

        clone_tree(staged_keg, dst, use_clonefile=True)  # Forced -> falls back

        assert (dst / "bin" / "openssl").read_bytes() == b"MACHO-binary"

    def test_a_real_error_is_not_swallowed_as_a_fallback(
        self, staged_keg, tmp_path, clonefile_fails
    ) -> None:
        """Test that only ENOTSUP means "try the slow path"; the rest are failures."""
        clonefile_fails(errno.EACCES)

        with pytest.raises(OSError) as exc:
            clone_tree(staged_keg, tmp_path / "out", use_clonefile=True)

        assert exc.value.errno == errno.EACCES


class TestInstallToCellar:
    """Tests pouring a staged keg into the Cellar and repointing its opt link."""

    def test_the_keg_lands_with_a_relative_opt_link(
        self, staged_keg, prefix_path
    ) -> None:
        """Test that a keg is placed in the Cellar and opt points at it relatively."""
        dest = _install(staged_keg, prefix_path)
        assert dest == prefix_path / "Cellar" / "openssl@3" / "3.0"
        assert (dest / "bin" / "openssl").read_bytes() == b"MACHO-binary"

        opt = prefix_path / "opt" / "openssl@3"
        assert opt.is_symlink()
        assert os.readlink(opt) == "../Cellar/openssl@3/3.0"  # Relative
        assert opt.resolve() == dest.resolve()

    def test_reinstalling_replaces_a_read_only_keg(
        self, staged_keg, prefix_path
    ) -> None:
        """Test that the same version reinstalls over itself, read-only files and all."""
        _install(staged_keg, prefix_path)

        # Mutate the staged source, reinstall the same version, expect replacement
        (staged_keg / "bin" / "openssl").chmod(0o755)
        (staged_keg / "bin" / "openssl").write_bytes(b"REBUILT")
        (staged_keg / "bin" / "openssl").chmod(0o555)
        dest = _install(staged_keg, prefix_path)

        assert (dest / "bin" / "openssl").read_bytes() == b"REBUILT"

    def test_an_upgrade_repoints_opt_and_keeps_the_old_keg(
        self, staged_keg, prefix_path, tmp_path, build_keg
    ) -> None:
        """Test that upgrading moves the opt link without removing the old version."""
        _install(staged_keg, prefix_path, version="3.0")
        new = build_keg(tmp_path / "stage2" / "openssl@3" / "3.1")
        (new / "bin" / "openssl").chmod(0o755)
        (new / "bin" / "openssl").write_bytes(b"v3.1")
        (new / "bin" / "openssl").chmod(0o555)

        _install(new, prefix_path, version="3.1")

        opt = prefix_path / "opt" / "openssl@3"
        assert os.readlink(opt) == "../Cellar/openssl@3/3.1"
        assert (prefix_path / "Cellar" / "openssl@3" / "3.0").exists()  # Retained
        assert (
            prefix_path / "Cellar" / "openssl@3" / "3.1" / "bin" / "openssl"
        ).read_bytes() == b"v3.1"

    def test_a_dangling_opt_link_is_refreshed(self, staged_keg, prefix_path) -> None:
        """Test that opt left pointing at a removed keg is repaired, not tripped over."""
        opt = prefix_path / "opt" / "openssl@3"
        opt.parent.mkdir(parents=True)
        opt.symlink_to(Path("..") / "Cellar" / "openssl@3" / "9.9")  # Points at nothing

        _install(staged_keg, prefix_path)

        assert os.readlink(opt) == "../Cellar/openssl@3/3.0"

    def test_a_partial_keg_is_cleaned_up_on_failure(
        self, staged_keg, prefix_path, monkeypatch
    ) -> None:
        """Test that a copy failing halfway leaves no half-poured keg behind."""

        def half_then_fail(src, dst, *, use_clonefile=None) -> None:
            dst.mkdir(parents=True)
            (dst / "partial").write_bytes(b"x")
            raise OSError(errno.EIO, "disk error")

        monkeypatch.setattr(_cellar, "clone_tree", half_then_fail)

        with pytest.raises(CellarError):
            _install(staged_keg, prefix_path)

        assert not (prefix_path / "Cellar" / "openssl@3" / "3.0").exists()


class TestRemoveRack:
    """Tests for remove_rack, the native removal of one formula's kegs."""

    def test_refuses_a_locked_rack(self, tmp_path) -> None:
        """Test that the rack lock is really taken: a peer's hold keeps the kegs in place."""
        from brewery.core.errors import OperationInProgressError

        cellar = tmp_path / "Cellar" / "tool"
        (cellar / "1.0" / "bin").mkdir(parents=True)
        prefix = tmp_path / "prefix"

        with held_rack(prefix, "tool"), pytest.raises(OperationInProgressError):
            remove_rack(cellar, prefix, "tool")

        assert cellar.exists()

    def test_missing_dir_is_noop(self, tmp_path) -> None:
        """Test that a missing cellar dir is a clean no-op (already-removed success path)."""
        remove_rack(tmp_path / "Cellar" / "ghost", tmp_path / "prefix", "ghost")

    def test_unlinks_all_versions_then_removes(self, tmp_path, monkeypatch) -> None:
        """Test that every version keg is unlinked before the formula's cellar dir is removed."""
        cellar = tmp_path / "Cellar" / "tool"
        (cellar / "1.0" / "bin").mkdir(parents=True)
        (cellar / "2.0" / "bin").mkdir(parents=True)
        seen: list[str] = []
        monkeypatch.setattr(
            _cellar, "unlink_keg", lambda keg, *, prefix, name: seen.append(keg.name)
        )
        remove_rack(cellar, tmp_path / "prefix", "tool")
        assert sorted(seen) == ["1.0", "2.0"]
        assert not cellar.exists()

    def test_removes_a_read_only_keg(self, tmp_path, monkeypatch) -> None:
        """Test that bottles ship read-only files, so removal must go through cellar.rmtree."""
        cellar = tmp_path / "Cellar" / "tool"
        share = cellar / "1.0" / "share"
        share.mkdir(parents=True)
        (share / "data").write_text("x")
        os.chmod(share / "data", 0o444)
        os.chmod(share, 0o555)

        monkeypatch.setattr(_cellar, "unlink_keg", lambda keg, *, prefix, name: None)
        remove_rack(cellar, tmp_path / "prefix", "tool")

        assert not cellar.exists()
