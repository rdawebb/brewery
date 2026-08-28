"""Tests for restoring a bottle's etc/var config into the prefix."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brewery.providers.bottle_config import install_etc_var


def _keg(cellar: Path, version: str = "1.0", *, files: dict[str, str] | None = None):
    """Build a keg with a `.bottle` tree at `cellar/pkg/<version>`.

    Args:
        cellar: The Cellar directory to build under.
        version: The keg version directory name.
        files: `.bottle`-relative paths mapped to their contents.

    Returns:
        The created keg version directory.
    """
    keg = cellar / "pkg" / version
    keg.mkdir(parents=True)

    for rel, text in (files or {}).items():
        path = keg / ".bottle" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    return keg


class TestInstallEtcVar:
    """Tests for `install_etc_var`."""

    def test_no_bottle_dir_is_a_no_op(self, tmp_path) -> None:
        """Test that a keg without a `.bottle` tree leaves the prefix untouched."""
        prefix = tmp_path / "prefix"
        prefix.mkdir()
        keg = _keg(tmp_path / "Cellar")

        result = install_etc_var(keg, prefix=prefix)

        assert result.copied == []
        assert result.defaults == []
        assert list(prefix.iterdir()) == []

    def test_new_files_are_copied_as_real_files(self, tmp_path) -> None:
        """Test that bottled etc and var files land in the prefix as real files."""
        prefix = tmp_path / "prefix"
        keg = _keg(
            tmp_path / "Cellar",
            files={"etc/pkg.conf": "default\n", "var/pkg/seed": "seed\n"},
        )

        result = install_etc_var(keg, prefix=prefix)

        conf = prefix / "etc" / "pkg.conf"
        assert conf.read_text() == "default\n"
        assert not conf.is_symlink()
        assert (prefix / "var" / "pkg" / "seed").read_text() == "seed\n"
        assert sorted(result.copied) == ["etc/pkg.conf", "var/pkg/seed"]
        assert result.defaults == []

    def test_file_mode_is_carried_across(self, tmp_path) -> None:
        """Test that an executable bottled file stays executable in the prefix."""
        prefix = tmp_path / "prefix"
        keg = _keg(tmp_path / "Cellar", files={"etc/misc/CA.pl": "#!/usr/bin/perl\n"})
        os.chmod(keg / ".bottle" / "etc" / "misc" / "CA.pl", 0o755)

        install_etc_var(keg, prefix=prefix)

        assert (prefix / "etc" / "misc" / "CA.pl").stat().st_mode & 0o777 == 0o755

    def test_empty_directories_are_created(self, tmp_path) -> None:
        """Test that directories a bottle ships empty are reserved in the prefix."""
        prefix = tmp_path / "prefix"
        keg = _keg(tmp_path / "Cellar")
        (keg / ".bottle" / "var" / "cache" / "pkg").mkdir(parents=True)

        install_etc_var(keg, prefix=prefix)

        assert (prefix / "var" / "cache" / "pkg").is_dir()

    def test_identical_config_is_left_alone(self, tmp_path) -> None:
        """Test that a prefix config already matching the bottle is not rewritten."""
        prefix = tmp_path / "prefix"
        keg = _keg(tmp_path / "Cellar", files={"etc/pkg.conf": "default\n"})
        (prefix / "etc").mkdir(parents=True)
        (prefix / "etc" / "pkg.conf").write_text("default\n")
        before = (prefix / "etc" / "pkg.conf").stat().st_mtime_ns

        result = install_etc_var(keg, prefix=prefix)

        assert (prefix / "etc" / "pkg.conf").stat().st_mtime_ns == before
        assert result.copied == []
        assert result.defaults == []

    def test_edited_config_gets_a_default_neighbour(self, tmp_path) -> None:
        """Test that an edited config survives; the new default lands beside it."""
        prefix = tmp_path / "prefix"
        keg = _keg(tmp_path / "Cellar", version="2.0", files={"etc/pkg.conf": "new\n"})
        (prefix / "etc").mkdir(parents=True)
        (prefix / "etc" / "pkg.conf").write_text("mine\n")

        result = install_etc_var(keg, prefix=prefix)

        assert (prefix / "etc" / "pkg.conf").read_text() == "mine\n"
        assert (prefix / "etc" / "pkg.conf.default").read_text() == "new\n"
        assert result.defaults == ["etc/pkg.conf.default"]
        assert result.copied == []

    def test_untouched_older_default_is_advanced_in_place(self, tmp_path) -> None:
        """Test that a config still matching an older keg's default is replaced, not shadowed."""
        cellar = tmp_path / "Cellar"
        prefix = tmp_path / "prefix"
        _keg(cellar, version="1.0", files={"etc/pkg.conf": "old\n"})
        new = _keg(cellar, version="2.0", files={"etc/pkg.conf": "new\n"})
        (prefix / "etc").mkdir(parents=True)
        (prefix / "etc" / "pkg.conf").write_text("old\n")

        result = install_etc_var(new, prefix=prefix)

        assert (prefix / "etc" / "pkg.conf").read_text() == "new\n"
        assert not (prefix / "etc" / "pkg.conf.default").exists()
        assert result.copied == ["etc/pkg.conf"]

    def test_sibling_lookup_ignores_a_different_relative_path(self, tmp_path) -> None:
        """Test that a sibling default under another name does not license an overwrite."""
        cellar = tmp_path / "Cellar"
        prefix = tmp_path / "prefix"
        _keg(cellar, version="1.0", files={"etc/other.conf": "old\n"})
        new = _keg(cellar, version="2.0", files={"etc/pkg.conf": "new\n"})
        (prefix / "etc").mkdir(parents=True)
        (prefix / "etc" / "pkg.conf").write_text("old\n")

        install_etc_var(new, prefix=prefix)

        assert (prefix / "etc" / "pkg.conf").read_text() == "old\n"
        assert (prefix / "etc" / "pkg.conf.default").read_text() == "new\n"

    def test_unwritable_prefix_raises(self, tmp_path) -> None:
        """Test that an unwritable prefix directory surfaces as an OSError."""
        prefix = tmp_path / "prefix"
        (prefix / "etc").mkdir(parents=True)
        os.chmod(prefix / "etc", 0o500)
        keg = _keg(tmp_path / "Cellar", files={"etc/pkg.conf": "default\n"})

        try:
            with pytest.raises(OSError):
                install_etc_var(keg, prefix=prefix)

        finally:
            os.chmod(prefix / "etc", 0o755)
