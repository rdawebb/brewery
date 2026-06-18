"""Unit tests for the uninstall assembly function."""

from __future__ import annotations

import brewery.providers.uninstall_service as svc
from brewery.core.errors import BrewCommandError


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

    async def uninstall(self, names: list[str]) -> None:
        """Record the uninstall call and optionally raise a failure.

        Args:
            names: The list of formula names to uninstall

        Raises:
            BrewCommandError: Always raised to force the brew fallback
        """
        self.calls.append(names)
        if self.fail:
            raise BrewCommandError("brew uninstall failed")


class MockRepo:
    """Repo stub exposing only the formula backend run_uninstall touches."""

    def __init__(self, fail: bool = False) -> None:
        """Initialise the repo with an optional failure mode.

        Args:
            fail: Whether to fail on uninstall calls (default: False)
        """
        self.formula = RecordingBackend(fail=fail)


async def test_native_success_takes_no_fallback(mock_env, monkeypatch) -> None:
    """Every formula removed natively means the provider is never called."""
    seen: list[str] = []
    monkeypatch.setattr(svc, "_remove_formula", lambda c, p, name: seen.append(name))
    repo = MockRepo()
    await svc.run_uninstall(repo, ["yazi", "act"], env=mock_env)
    assert seen == ["yazi", "act"]
    assert repo.formula.calls == []


async def test_native_failure_falls_back_per_formula(mock_env, monkeypatch) -> None:
    """A native OSError for one formula falls back to brew for that one only."""

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

    monkeypatch.setattr(svc, "_remove_formula", remove)
    repo = MockRepo()
    await svc.run_uninstall(repo, ["yazi", "act"], env=mock_env)
    assert repo.formula.calls == [["act"]]  # yazi handled natively, only act fell back


async def test_brew_fallback_failure_is_swallowed(mock_env, monkeypatch) -> None:
    """A failing brew fallback does not propagate (verify reports the survivor)."""
    monkeypatch.setattr(svc, "_remove_formula", _raise_os)
    repo = MockRepo(fail=True)
    await svc.run_uninstall(repo, ["yazi"], env=mock_env)  # Should not raise
    assert repo.formula.calls == [["yazi"]]


async def test_env_resolved_when_omitted(mock_env, monkeypatch) -> None:
    """Omitting env= falls back to get_brewery_env() for the cellar/prefix paths."""
    monkeypatch.setattr(svc, "get_brewery_env", lambda: mock_env)
    seen: list[tuple] = []
    monkeypatch.setattr(svc, "_remove_formula", lambda c, p, name: seen.append((c, p)))
    await svc.run_uninstall(MockRepo(), ["yazi"])  # No env=
    assert seen == [(mock_env.cellar / "yazi", mock_env.prefix)]


def test_remove_formula_missing_dir_is_noop(tmp_path) -> None:
    """A missing cellar dir is a clean no-op (already-removed success path)."""
    svc._remove_formula(tmp_path / "Cellar" / "ghost", tmp_path / "prefix", "ghost")


def test_remove_formula_unlinks_all_versions_then_removes(
    tmp_path, monkeypatch
) -> None:
    """Every version keg is unlinked before the formula's cellar dir is removed."""
    cellar = tmp_path / "Cellar" / "tool"
    (cellar / "1.0" / "bin").mkdir(parents=True)
    (cellar / "2.0" / "bin").mkdir(parents=True)
    seen: list[str] = []
    monkeypatch.setattr(
        svc, "unlink_keg", lambda keg, *, prefix, name: seen.append(keg.name)
    )
    svc._remove_formula(cellar, tmp_path / "prefix", "tool")
    assert sorted(seen) == ["1.0", "2.0"]
    assert not cellar.exists()
