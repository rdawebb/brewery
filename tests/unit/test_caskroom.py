"""Unit tests for staging and moving cask artifacts."""

from __future__ import annotations

import sys

import pytest

from brewery.core.errors import LinkError
from brewery.providers.caskroom import _move_out

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="native casks are macOS-only"
)


def test_move_out_names_a_links_destination(tmp_path) -> None:
    """Test that a symlink in the target's way is reported by what it points at."""
    source = tmp_path / "Foo.app"
    source.mkdir()
    target = tmp_path / "apps" / "Foo.app"
    target.parent.mkdir()
    target.symlink_to("/elsewhere/Foo.app")

    with pytest.raises(LinkError) as err:
        _move_out(source, target)

    assert str(target) in str(err.value)
    assert "/elsewhere/Foo.app" in str(err.value)
    assert source.is_dir()
