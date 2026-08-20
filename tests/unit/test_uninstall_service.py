"""Unit tests for the native uninstall runner."""

from __future__ import annotations

import brewery.services.uninstall as svc
from brewery.core.errors import BrewCommandError, OperationInProgressError


def _raise_os(c, p, name) -> None:
    """Helper: always raise OSError, to force the brew fallback.

    Args:
        c: The cellar directory
        p: The prefix directory
        name: The name of the formula

    Raises:
        OSError: Always raised to force the brew fallback
    """
    raise OSError("native failed")


class RecordingBackend:
    """Formula-backend stub recording uninstall calls; optionally fails."""

    def __init__(self, fail: bool = False) -> None:
        """Initialise the backend with an optional failure mode.

        Args:
            fail: Whether to fail on uninstall calls (default: False)
        """
        self.calls: list[list[str]] = []
        self.fail = fail

    async def uninstall(self, names: list[str]) -> list[str]:
        """Record the uninstall call and optionally raise a failure.

        Args:
            names: The list of formula names to uninstall

        Returns:
            The names unchanged.

        Raises:
            BrewCommandError: Always raised to force the brew fallback
        """
        self.calls.append(names)
        if self.fail:
            raise BrewCommandError("brew uninstall failed")

        return names


async def test_native_success_takes_no_fallback(mock_env, monkeypatch) -> None:
    """Test that every formula removed natively means the provider is never called."""
    seen: list[str] = []
    monkeypatch.setattr(svc, "remove_rack", lambda c, p, name: seen.append(name))
    formula = RecordingBackend()
    await svc.run_uninstall(["yazi", "act"], formula=formula, env=mock_env)
    assert seen == ["yazi", "act"]
    assert formula.calls == []


async def test_native_failure_falls_back_per_formula(mock_env, monkeypatch) -> None:
    """Test that a native OSError for one formula falls back to brew for that one only."""

    def remove(c, p, name) -> None:
        """Raise OSError for 'act' to test native fallback, otherwise no-op.

        Args:
            c: The cellar directory
            p: The prefix directory
            name: The name of the formula

        Raises:
            OSError: If the name is 'act', to test native fallback
        """
        if name == "act":
            raise OSError("native failed")

    monkeypatch.setattr(svc, "remove_rack", remove)
    formula = RecordingBackend()
    await svc.run_uninstall(["yazi", "act"], formula=formula, env=mock_env)
    assert formula.calls == [["act"]]  # yazi handled natively, only act fell back


async def test_brew_fallback_failure_is_swallowed(mock_env, monkeypatch) -> None:
    """Test that a failing brew fallback does not propagate (verify reports the survivor)."""
    monkeypatch.setattr(svc, "remove_rack", _raise_os)
    formula = RecordingBackend(fail=True)
    await svc.run_uninstall(["yazi"], formula=formula, env=mock_env)  # Should not raise
    assert formula.calls == [["yazi"]]


async def test_env_resolved_when_omitted(mock_env, monkeypatch) -> None:
    """Test that omitting env= falls back to get_brewery_env() for the cellar/prefix paths."""
    monkeypatch.setattr(svc, "get_brewery_env", lambda: mock_env)
    seen: list[tuple] = []
    monkeypatch.setattr(svc, "remove_rack", lambda c, p, name: seen.append((c, p)))
    await svc.run_uninstall(["yazi"], formula=RecordingBackend())  # No env=
    assert seen == [(mock_env.cellar / "yazi", mock_env.prefix)]


async def test_locked_rack_skips_the_brew_fallback(mock_env, monkeypatch) -> None:
    """Test that brew locks the same rack, so falling back to it would fail identically."""

    def remove(c, p, name) -> None:
        """Raise as though a peer process held the rack lock.

        Args:
            c: The cellar directory
            p: The prefix directory
            name: The name of the formula

        Raises:
            OperationInProgressError: Always, standing in for a locked rack
        """
        raise OperationInProgressError(str(c))

    monkeypatch.setattr(svc, "remove_rack", remove)
    formula = RecordingBackend()
    await svc.run_uninstall(["yazi"], formula=formula, env=mock_env)  # Should not raise

    assert formula.calls == []
