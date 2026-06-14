"""Tests for the unified brew provider backends.

run_brew is Mocked to return crafted BrewResults, so these verify the backend
factory passes the right kind flags and that the "already installed" / "pinned"
messages map to typed warnings only on a non-zero exit.
"""

from __future__ import annotations

import pytest

import brewery.providers.brew as brew
from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewCommandError,
    PinnedPackageWarning,
)
from brewery.core.shell import BrewResult
from brewery.providers.brew import cask_backend, formula_backend

pytestmark = pytest.mark.asyncio


def _mock_run(monkeypatch, result: BrewResult):
    """Patch ``brew.run_brew`` with a stub that records calls and returns *result*.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        result: The BrewResult the stub should return on every call.

    Returns:
        A list that accumulates one dict per call with keys ``args``, ``output``,
        and ``check``.
    """
    calls = []

    async def Mock(args, *, output, check):
        """Record the call and return the pre-configured result.

        Args:
            args: The brew argument list.
            output: The BrewOutput mode.
            check: Whether to raise on non-zero exit.

        Returns:
            The pre-configured BrewResult.
        """
        calls.append({"args": args, "output": output, "check": check})
        return result

    monkeypatch.setattr(brew, "run_brew", Mock)

    return calls


async def test_formula_install_passes_formula_flag(monkeypatch):
    """Test that formula_backend.install passes --formula to brew."""
    calls = _mock_run(monkeypatch, BrewResult("", "", 0))
    assert await formula_backend.install(["wget"]) == ["wget"]
    assert calls[0]["args"] == ["install", "--formula", "wget"]
    assert calls[0]["check"] is False  # Backend inspects the result itself


async def test_cask_install_passes_cask_flag(monkeypatch):
    """Test that cask_backend.install passes --cask to brew."""
    calls = _mock_run(monkeypatch, BrewResult("", "", 0))
    await cask_backend.install(["firefox"])
    assert calls[0]["args"] == ["install", "--cask", "firefox"]


async def test_upgrade_passes_no_kind_flag(monkeypatch):
    """Test that upgrade passes no kind flag, as brew infers the package type."""
    calls = _mock_run(monkeypatch, BrewResult("", "", 0))
    await formula_backend.upgrade(["wget"])
    assert calls[0]["args"] == ["upgrade", "wget"]


async def test_already_installed_maps_to_warning(monkeypatch):
    """Test that an 'already installed' message on non-zero exit raises AlreadyInstalledWarning."""
    _mock_run(monkeypatch, BrewResult("Warning: wget already installed", "", 1))
    with pytest.raises(AlreadyInstalledWarning) as ei:
        await formula_backend.install(["wget"])
    assert "wget" in ei.value.context["package"]


async def test_pinned_upgrade_maps_to_warning(monkeypatch):
    """Test that a 'pinned' message on non-zero exit raises PinnedPackageWarning."""
    _mock_run(monkeypatch, BrewResult("", "Error: openssl is pinned", 1))
    with pytest.raises(PinnedPackageWarning):
        await formula_backend.upgrade(["openssl"])


async def test_generic_nonzero_raises_command_error(monkeypatch):
    """Test that an unrecognised non-zero exit raises BrewCommandError."""
    _mock_run(monkeypatch, BrewResult("", "network error", 1))
    with pytest.raises(BrewCommandError):
        await formula_backend.install(["wget"])


async def test_already_installed_message_on_success_is_not_warning(monkeypatch):
    """Test that an 'already installed' message on a zero exit is not an error."""
    # rc 0 -> success even if the text mentions "already installed"
    _mock_run(monkeypatch, BrewResult("Warning: wget already installed", "", 0))
    assert await formula_backend.install(["wget"]) == ["wget"]
