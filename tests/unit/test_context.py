"""Unit tests for the CLI's shared runtime context."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

import brewery.cli.output as output_mod
import brewery.daemon.catalog_refresh as refresh_mod
from brewery.cli import context as context_mod
from brewery.cli.context import _ensure_catalog_populated, _repository, run_async

if TYPE_CHECKING:
    from brewery.core.repo import Repository


@pytest.fixture
def bootstrap(monkeypatch) -> SimpleNamespace:
    """Stub the catalog bootstrap's two collaborators and record what ran.

    Both are imported inside the function body, so they are patched on their
    defining modules rather than on `cli.context`.

    Args:
        monkeypatch: The monkeypatch fixture.

    Returns:
        A namespace with `refreshed` (catalogs passed to refresh_catalog) and
        `spun` (spinner messages shown).
    """
    state = SimpleNamespace(refreshed=[], spun=[])

    async def _refresh(*, catalog) -> None:
        state.refreshed.append(catalog)

    @contextmanager
    def _spinner(message, **kwargs):
        state.spun.append(message)
        yield

    monkeypatch.setattr(refresh_mod, "refresh_catalog", _refresh)
    monkeypatch.setattr(output_mod, "spinner", _spinner)

    return state


def _repo(*, empty: bool) -> Repository:
    """A stand-in repository whose catalog reports the given emptiness.

    Args:
        empty: What `catalog.is_empty()` should report.

    Returns:
        An object with the single attribute the bootstrap reads.
    """
    fake = SimpleNamespace(catalog=SimpleNamespace(is_empty=lambda: empty))

    return cast("Repository", fake)


class TestEnsureCatalogPopulated:
    """Tests the first-run bootstrap, and the swallow that keeps it best-effort."""

    def test_an_empty_catalog_is_refreshed_in_the_foreground(self, bootstrap) -> None:
        """Test that a first run populates the catalog before the command proceeds.

        Without this, a fresh install's `brewery list` reports nothing outdated
        because there is nothing to compare against.
        """
        repo = _repo(empty=True)

        _ensure_catalog_populated(repo)

        assert bootstrap.refreshed == [repo.catalog]
        assert bootstrap.spun, "no progress was shown for a foreground refresh"

    def test_a_populated_catalog_is_left_alone(self, bootstrap) -> None:
        """Test that every ordinary command skips the bootstrap entirely."""
        _ensure_catalog_populated(_repo(empty=False))

        assert bootstrap.refreshed == []
        assert bootstrap.spun == []

    def test_a_failed_bootstrap_does_not_take_the_command_down(
        self, monkeypatch, bootstrap
    ) -> None:
        """Test that a first-run network failure is swallowed, not raised.

        The command still runs against an empty catalog. That is deliberate,
        but is also why the failure has to be logged.
        """
        warnings: list[dict] = []
        monkeypatch.setattr(
            context_mod.log, "warning", lambda **kw: warnings.append(kw), raising=False
        )

        async def _boom(*, catalog) -> None:
            raise ConnectionError("no network")

        monkeypatch.setattr(refresh_mod, "refresh_catalog", _boom)

        _ensure_catalog_populated(_repo(empty=True))

        assert warnings == [
            {"event": "catalog_bootstrap_failed", "error": "no network"}
        ]


class TestRepository:
    """Tests the context manager every command opens its repository through."""

    @pytest.fixture
    def opened(self, monkeypatch) -> SimpleNamespace:
        """Substitute Repository and the bootstrap, and record the lifecycle.

        Args:
            monkeypatch: The monkeypatch fixture.

        Returns:
            A namespace with the built `repo` and the `events` it recorded.
        """
        events: list[str] = []
        repo = SimpleNamespace(close=lambda: events.append("close"))

        monkeypatch.setattr(
            context_mod, "Repository", lambda: events.append("build") or repo
        )
        monkeypatch.setattr(
            context_mod,
            "_ensure_catalog_populated",
            lambda r: events.append("bootstrap"),
        )

        return SimpleNamespace(repo=repo, events=events)

    def test_the_catalog_is_bootstrapped_before_the_body_runs(self, opened) -> None:
        """Test that a command never sees a repository with an unbuilt catalog."""
        with _repository() as repo:
            opened.events.append("body")

            assert repo is opened.repo

        assert opened.events == ["build", "bootstrap", "body", "close"]

    def test_the_repository_is_closed_even_when_the_body_raises(self, opened) -> None:
        """Test that a failing command still releases its database handle."""
        with pytest.raises(ValueError, match="boom"), _repository():
            raise ValueError("boom")

        assert opened.events[-1] == "close"


class TestRunAsync:
    """Tests run_async, the one place the CLI enters an event loop."""

    def test_the_coroutine_result_is_returned(self) -> None:
        """Test that run_async hands back what the coroutine produced."""

        async def _answer() -> int:
            return 42

        assert run_async(_answer()) == 42

    def test_an_exception_propagates_to_the_caller(self) -> None:
        """Test that run_async does not swallow failures from the coroutine."""

        async def _boom() -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError, match="nope"):
            run_async(_boom())
