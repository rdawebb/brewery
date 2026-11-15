"""Unit tests for staging and moving cask artifacts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from brewery.core.errors import LinkError
from brewery.providers import caskroom
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


def test_signer_reads_the_requirement_without_verifying(monkeypatch) -> None:
    """Test that the old app's signer takes one `codesign -d`, not a deep verify too."""
    calls: list[list[str]] = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        out = "Executable=/x\ndesignated => identifier foo and anchor apple generic\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(caskroom.subprocess, "run", run)

    signer = caskroom._signer(Path("/Applications/Foo.app"))

    assert signer == "identifier foo and anchor apple generic"
    assert calls == [["codesign", "-d", "-r-", "--", "/Applications/Foo.app"]]
