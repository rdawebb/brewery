"""Unit tests for the pinned-keg bookkeeping writer."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brewery.providers.pin_service import is_pinned, pin, pin_path, unpin

pytestmark = pytest.mark.unit


@pytest.fixture
def prefix_and_keg(mock_env, make_keg) -> tuple[Path, Path]:
    """A hermetic prefix holding one installed keg."""
    keg = make_keg(mock_env.cellar, "wget", "1.25.0")

    return mock_env.prefix, keg


class TestPin:
    """Tests for writing the pinned-keg record."""

    def test_pin_writes_a_relative_symlink_to_the_keg(self, prefix_and_keg) -> None:
        """The record is a relative symlink resolving to the pinned keg."""
        prefix, keg = prefix_and_keg

        assert pin(prefix=prefix, name="wget", keg=keg) is True

        record = pin_path(prefix=prefix, name="wget")
        assert record.is_symlink()
        assert os.readlink(record) == "../../../Cellar/wget/1.25.0"
        assert record.resolve() == keg.resolve()

    def test_pin_lands_in_brews_pinned_dir(self, prefix_and_keg) -> None:
        """The record goes where fs_state.pinned_names reads from."""
        prefix, keg = prefix_and_keg
        pin(prefix=prefix, name="wget", keg=keg)

        assert (prefix / "var" / "homebrew" / "pinned" / "wget").is_symlink()

    def test_pinning_twice_is_a_no_op(self, prefix_and_keg) -> None:
        """A second pin reports False and leaves the existing record intact."""
        prefix, keg = prefix_and_keg
        pin(prefix=prefix, name="wget", keg=keg)
        before = os.readlink(pin_path(prefix=prefix, name="wget"))

        assert pin(prefix=prefix, name="wget", keg=keg) is False
        assert os.readlink(pin_path(prefix=prefix, name="wget")) == before

    def test_is_pinned_tracks_the_record(self, prefix_and_keg) -> None:
        """is_pinned flips with the record's existence."""
        prefix, keg = prefix_and_keg

        assert is_pinned(prefix=prefix, name="wget") is False
        pin(prefix=prefix, name="wget", keg=keg)
        assert is_pinned(prefix=prefix, name="wget") is True


class TestUnpin:
    """Tests for removing the pinned-keg record."""

    def test_unpin_removes_the_record(self, prefix_and_keg) -> None:
        """Unpinning drops the symlink and reports it did so."""
        prefix, keg = prefix_and_keg
        pin(prefix=prefix, name="wget", keg=keg)

        assert unpin(prefix=prefix, name="wget") is True
        assert not pin_path(prefix=prefix, name="wget").exists()

    def test_unpin_of_an_unpinned_formula_is_a_no_op(self, prefix_and_keg) -> None:
        """Unpinning what was never pinned reports False rather than raising."""
        prefix, _ = prefix_and_keg

        assert unpin(prefix=prefix, name="wget") is False

    def test_unpin_leaves_the_keg_alone(self, prefix_and_keg) -> None:
        """Removing the record must not follow the symlink into the Cellar."""
        prefix, keg = prefix_and_keg
        pin(prefix=prefix, name="wget", keg=keg)
        unpin(prefix=prefix, name="wget")

        assert keg.is_dir()

    def test_pinned_dir_survives_the_last_unpin(self, prefix_and_keg) -> None:
        """The dir stays put, keeping the cache token's stat set stable."""
        prefix, keg = prefix_and_keg
        pin(prefix=prefix, name="wget", keg=keg)
        unpin(prefix=prefix, name="wget")

        assert (prefix / "var" / "homebrew" / "pinned").is_dir()
