"""Unit tests for run_brew_command's result/error mapping over a faked run_capture."""

from __future__ import annotations

import pytest

from brewery.core import shell as shell_mod
from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewCommandError,
    PinnedPackageWarning,
)
from brewery.core.shell import run_brew_command

pytestmark = pytest.mark.unit


def _fake_capture(out: str = "", err: str = "", code: int = 0):
    """Build an async run_capture stub returning a fixed (out, err, code).

    Args:
        out: The standard output to return.
        err: The standard error to return.
        code: The exit code to return.

    Returns:
        An async function that simulates the behavior of run_capture.
    """

    async def _capture(*cmd, timeout=None) -> tuple[str, str, int]:
        """Simulate the behavior of run_capture.

        Args:
            *cmd: The command arguments.
            timeout: The timeout for the command.

        Returns:
            A tuple containing the standard output, standard error, and exit code.
        """
        return out, err, code

    return _capture


class TestSuccess:
    """Tests for the success path."""

    async def test_returns_capture_tuple(self, monkeypatch) -> None:
        """Test that a zero exit returns the (out, err, code) tuple."""
        monkeypatch.setattr(shell_mod, "run_capture", _fake_capture(out="done", code=0))
        out, err, code = await run_brew_command("install", ["wget"], ["--formula"])
        assert (out, err, code) == ("done", "", 0)


class TestAlreadyInstalled:
    """Tests for the already-installed warning branch."""

    async def test_install_already_installed_raises(self, monkeypatch) -> None:
        """Test that install + 'already installed' raises AlreadyInstalledWarning."""
        monkeypatch.setattr(
            shell_mod,
            "run_capture",
            _fake_capture(err="Warning: wget already installed", code=1),
        )
        with pytest.raises(AlreadyInstalledWarning) as exc:
            await run_brew_command("install", ["wget"], ["--formula"])
        assert "wget" in exc.value.context["package"]

    async def test_only_matched_names_reported(self, monkeypatch) -> None:
        """Test that only names present in the output are attributed."""
        monkeypatch.setattr(
            shell_mod,
            "run_capture",
            _fake_capture(out="curl already installed", code=1),
        )
        with pytest.raises(AlreadyInstalledWarning) as exc:
            await run_brew_command("install", ["wget", "curl"], ["--formula"])
        assert "curl" in exc.value.context["package"]
        assert "wget" not in exc.value.context["package"]

    async def test_not_triggered_for_uninstall(self, monkeypatch) -> None:
        """Test that the already-installed branch is install-only.

        The same output under uninstall must fall through to BrewCommandError.
        """
        monkeypatch.setattr(
            shell_mod,
            "run_capture",
            _fake_capture(err="already installed", code=1),
        )
        with pytest.raises(BrewCommandError):
            await run_brew_command("uninstall", ["wget"], ["--formula"])


class TestPinned:
    """Tests for the pinned warning branch."""

    async def test_upgrade_pinned_raises(self, monkeypatch) -> None:
        """Test that upgrade + 'pinned' raises PinnedPackageWarning."""
        monkeypatch.setattr(
            shell_mod, "run_capture", _fake_capture(err="wget is pinned", code=1)
        )
        with pytest.raises(PinnedPackageWarning) as exc:
            await run_brew_command("upgrade", ["wget"], [])
        assert "wget" in exc.value.context["package"]

    async def test_pinned_only_for_upgrade(self, monkeypatch) -> None:
        """Test that 'pinned' under install falls through to BrewCommandError."""
        monkeypatch.setattr(
            shell_mod, "run_capture", _fake_capture(err="pinned", code=1)
        )
        with pytest.raises(BrewCommandError):
            await run_brew_command("install", ["wget"], ["--formula"])


class TestFailure:
    """Tests for the generic failure path."""

    async def test_nonzero_raises_brew_command_error(self, monkeypatch) -> None:
        """Test that a nonzero exit with no special marker raises BrewCommandError."""
        monkeypatch.setattr(shell_mod, "run_capture", _fake_capture(err="boom", code=2))
        with pytest.raises(BrewCommandError) as exc:
            await run_brew_command("install", ["wget"], ["--formula"])
        assert exc.value.context.get("returncode") == 2

    async def test_error_prefers_stderr(self, monkeypatch) -> None:
        """Test that the error text uses stderr when present."""
        monkeypatch.setattr(
            shell_mod,
            "run_capture",
            _fake_capture(out="stdout text", err="stderr text", code=1),
        )
        with pytest.raises(BrewCommandError) as exc:
            await run_brew_command("uninstall", ["wget"], ["--formula"])
        assert "stderr text" in str(exc.value.context.get("error", ""))

    async def test_matching_is_case_insensitive(self, monkeypatch) -> None:
        """Test that marker matching ignores case (combined is lowercased)."""
        monkeypatch.setattr(
            shell_mod,
            "run_capture",
            _fake_capture(err="WGET ALREADY INSTALLED", code=1),
        )
        with pytest.raises(AlreadyInstalledWarning):
            await run_brew_command("install", ["wget"], ["--formula"])
