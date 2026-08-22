"""Integration test for the Cellar's real copy-on-write clone path."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from brewery.providers.cellar import clone_tree


@pytest.mark.skipif(sys.platform != "darwin", reason="clonefile is macOS-only")
def test_clonefile_matches_copytree(staged_keg, tmp_path) -> None:
    """Test that clonefile and copytree produce the same result."""
    via_clone = tmp_path / "clone"
    via_copy = tmp_path / "copy"
    clone_tree(staged_keg, via_clone, use_clonefile=True)
    clone_tree(staged_keg, via_copy, use_clonefile=False)

    def snapshot(root: Path) -> dict:
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
