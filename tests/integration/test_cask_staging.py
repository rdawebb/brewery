"""Integration tests for staging cask downloads into a brew-shaped Caskroom."""

from __future__ import annotations

import gzip
import json
import subprocess
import sys

import pytest
from _rows import cask_row
from _tar import make_tar

from brewery.core.errors import ExtractionError
from brewery.core.fs_state import scan_installed
from brewery.core.models import PackageKind
from brewery.providers.caskroom import QUARANTINE, stage

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="native casks are macOS-only"
)

ARTIFACTS = [
    {"uninstall": [{"quit": "org.example.foo"}]},
    {"app": ["Foo.app"], "target": "/Applications/Foo.app"},
]


def _quarantine(path) -> str:
    """Read a path's quarantine xattr, without following a symlink.

    Args:
        path: The path to read.

    Returns:
        The xattr's value, or an empty string if it has none.
    """
    proc = subprocess.run(
        ["xattr", "-s", "-p", QUARANTINE, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def _app(root) -> None:
    """Lay out a minimal `Foo.app` under `root`, with an internal symlink.

    Args:
        root: The directory to build the bundle in.
    """
    macos = root / "Foo.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "foo").write_text("#!/bin/sh\n")
    (root / "Foo.app" / "Contents" / "Current").symlink_to("MacOS")


def _row(url: str = "https://example.org/Foo-1.0.zip"):
    """A cask row for the synthetic `foo` cask.

    Args:
        url: The download URL, whose filename names a bare download.

    Returns:
        The CaskRow.
    """
    return cask_row("foo", version="1.0", url=url, artifacts=ARTIFACTS)


@pytest.fixture
def zipped(tmp_path):
    """A zip of `Foo.app`, made by ditto as vendors' zips are.

    Args:
        tmp_path: The pytest tmp_path fixture.

    Returns:
        The zip's path.
    """
    src = tmp_path / "src"
    _app(src)
    archive = tmp_path / "download"
    subprocess.run(
        ["ditto", "-c", "-k", "--keepParent", str(src / "Foo.app"), str(archive)],
        check=True,
    )
    return archive


class TestStage:
    """Tests for stage."""

    def test_zip_stages_with_symlinks_and_quarantine(self, brew, zipped) -> None:
        """Test that a zip unpacks into the version dir, quarantined throughout."""
        staged = stage(_row(), zipped, brew.caskroom)

        assert staged == brew.caskroom / "foo" / "1.0"
        assert (staged / "Foo.app" / "Contents" / "MacOS" / "foo").is_file()
        assert (staged / "Foo.app" / "Contents" / "Current").is_symlink()
        assert _quarantine(zipped).startswith("0081;")
        assert _quarantine(staged / "Foo.app").startswith("0181;")
        assert _quarantine(staged / "Foo.app" / "Contents" / "MacOS" / "foo")

    def test_metadata_matches_brews_layout(self, brew, zipped) -> None:
        """Test the caskfile and receipt brew reads, and that brewery lists it."""
        stage(_row(), zipped, brew.caskroom)
        metadata = brew.caskroom / "foo" / ".metadata"

        (caskfile,) = metadata.glob("1.0/*/Casks/foo.json")
        assert json.loads(caskfile.read_text()) == {}

        receipt = json.loads((metadata / "INSTALL_RECEIPT.json").read_text())
        assert receipt["source"]["version"] == "1.0"
        assert receipt["uninstall_artifacts"] == [
            {"uninstall": [{"quit": "org.example.foo"}]},
            {"app": ["Foo.app"]},
        ]

        casks = [r for r in scan_installed(brew.env) if r.kind is PackageKind.CASK]
        assert [(r.name, r.version) for r in casks] == [("foo", "1.0")]

    def test_tar_stages(self, brew, tmp_path) -> None:
        """Test that a gzipped tarball unpacks through the bottle extractor."""
        archive = tmp_path / "download"
        archive.write_bytes(
            gzip.compress(
                make_tar(
                    [
                        ("dir", "Foo.app", 0o755),
                        ("file", "Foo.app/Info.plist", b"<plist/>", 0o644),
                    ]
                )
            )
        )

        staged = stage(_row(), archive, brew.caskroom)

        assert (staged / "Foo.app" / "Info.plist").read_bytes() == b"<plist/>"

    def test_dmg_stages_without_volume_furniture(self, brew, tmp_path) -> None:
        """Test that a dmg's contents are copied out and the image detached."""
        src = tmp_path / "src"
        _app(src)
        (src / ".DS_Store").write_bytes(b"")
        (src / "Applications").symlink_to("/Applications")
        dmg = tmp_path / "Foo.dmg"
        subprocess.run(
            ["hdiutil", "create", "-quiet", "-fs", "HFS+", "-srcfolder", str(src),
             "-format", "UDZO", str(dmg)],
            check=True,
        )  # fmt: skip
        download = tmp_path / "download"
        dmg.rename(download)

        staged = stage(_row(), download, brew.caskroom)

        assert sorted(p.name for p in staged.iterdir()) == ["Foo.app"]
        assert (staged / "Foo.app" / "Contents" / "Current").is_symlink()
        info = subprocess.run(
            ["hdiutil", "info"], capture_output=True, text=True, check=True
        )
        assert str(download) not in info.stdout

    def test_bare_file_stages_under_its_url_name(self, brew, tmp_path) -> None:
        """Test that a naked download keeps the filename its artifact names."""
        download = tmp_path / "download"
        download.write_bytes(b"\x00\x01\x00\x00font")

        staged = stage(
            _row("https://example.org/fonts/Foo%5Bwght%5D.ttf"), download, brew.caskroom
        )

        assert (staged / "Foo[wght].ttf").read_bytes() == b"\x00\x01\x00\x00font"

    def test_unsupported_archive_raises_and_stages_nothing(
        self, brew, tmp_path
    ) -> None:
        """Test that a 7z goes back to brew, leaving no version behind."""
        download = tmp_path / "download"
        download.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)

        with pytest.raises(ExtractionError, match="7z"):
            stage(_row(), download, brew.caskroom)

        assert list((brew.caskroom / "foo").iterdir()) == []

    def test_already_staged_version_is_kept(self, brew, zipped) -> None:
        """Test that restaging a version raises rather than clobbering it."""
        staged = stage(_row(), zipped, brew.caskroom)

        with pytest.raises(ExtractionError, match="already staged"):
            stage(_row(), zipped, brew.caskroom)

        assert (staged / "Foo.app").is_dir()
        assert sorted(p.name for p in (brew.caskroom / "foo").iterdir()) == [
            ".metadata",
            "1.0",
        ]
