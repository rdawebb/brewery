"""Shared fixtures for Brewery unit tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _build_keg(version_dir: Path) -> Path:
    """Populate a minimal openssl@3-shaped keg at *version_dir* and return it."""
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
    """A staged openssl@3 3.0 keg tree ready for installation or relocation."""
    return _build_keg(tmp_path / "stage" / "openssl@3" / "3.0")


@pytest.fixture
def build_keg():
    """Return the keg-builder function for tests that need more than one keg."""
    return _build_keg


@pytest.fixture
def brew_paths() -> dict:
    """Standard Homebrew prefix/cellar/repository paths used across relocation tests."""
    return dict(
        prefix=Path("/opt/homebrew"),
        cellar=Path("/opt/homebrew/Cellar"),
        repository=Path("/opt/homebrew/Library/Homebrew"),
    )
