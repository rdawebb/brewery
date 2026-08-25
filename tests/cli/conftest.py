"""Fixtures for the CLI tests: a CliRunner wired to the fixture Repository.

`app` is driven through `CliRunner`, never `main(argv)`: `main` routes on its argv
argument and then calls a bare `app()`, which re-reads `sys.argv`, so a test
going through `main` would assert against whatever pytest was invoked with.
"""

from __future__ import annotations

import pkgutil
from contextlib import contextmanager
from importlib import import_module

import pytest
from _seams import REPO_FREE_MODULES
from typer.testing import CliRunner

import brewery.cli.commands as commands_pkg
from brewery.cli.context import app


@pytest.fixture
def mock_env(brew, monkeypatch):
    """Override the base mock_env with the same layout the integration tests use.

    Args:
        brew: The fresh Brew layout fixture.
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The mock environment.
    """
    from brewery.core import config

    brew.formula("yazi", "26.5.6", link_opt=False)
    brew.formula("act", "0.2.88", link_opt=False)
    brew.cask("iina", ["1.4.1,160"])

    monkeypatch.setattr(config, "_env_cache", brew.env)

    return brew.env


@pytest.fixture
def cli(monkeypatch, repo) -> CliRunner:
    """A CliRunner with every command's `_repository` yielding the fixture repo.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        repo: The Repository wired to a mock brew and a populated catalog.

    Returns:
        A CliRunner ready to invoke `app`.
    """

    @contextmanager
    def _fake_repository():
        yield repo

    missing: list[str] = []
    for info in pkgutil.iter_modules(commands_pkg.__path__):
        mod = import_module(f"{commands_pkg.__name__}.{info.name}")
        if hasattr(mod, "_repository"):
            monkeypatch.setattr(mod, "_repository", _fake_repository)

        elif info.name not in REPO_FREE_MODULES:
            missing.append(info.name)

    assert not missing, (
        f"command modules with no _repository seam: {sorted(missing)}. "
        "Either they stopped opening a repository (add them to REPO_FREE_MODULES) "
        "or they would run against the user's real prefix under test."
    )

    return CliRunner()


@pytest.fixture
def run(cli):
    """Invoke the real app with the given argv and return the Click result.

    Args:
        cli: The patched CliRunner fixture.

    Returns:
        A callable taking argv (and CliRunner kwargs) and returning the Result.
    """

    def _run(argv: list[str], **kwargs):
        return cli.invoke(app, argv, **kwargs)

    return _run
