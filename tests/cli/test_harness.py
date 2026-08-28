"""Tests that the CLI harness itself substitutes what it claims to."""

from __future__ import annotations

import pkgutil
from contextlib import contextmanager
from importlib import import_module

from _seams import REPO_FREE_MODULES

import brewery.cli.commands as commands_pkg


def test_a_command_receives_the_fixture_repository(run, repo, monkeypatch) -> None:
    """Test that the seam is the one a command body opens.

    Patching `cli.context._repository` would be a no-op, because every command
    binds the name locally at import.
    """
    seen: list[object] = []

    @contextmanager
    def _recording():
        seen.append(repo)
        yield repo

    monkeypatch.setattr(commands_pkg.query, "_repository", _recording)

    run(["list"])

    assert seen == [repo]


def test_every_command_module_is_patched_or_declared_repo_free() -> None:
    """Test that no command module can silently escape the harness.

    A new command that opens a repository but is not patched would run against
    a user's real prefix.
    """
    with_seam, without_seam = set(), set()
    for info in pkgutil.iter_modules(commands_pkg.__path__):
        mod = import_module(f"{commands_pkg.__name__}.{info.name}")
        (with_seam if hasattr(mod, "_repository") else without_seam).add(info.name)

    assert without_seam == set(REPO_FREE_MODULES)
    assert with_seam, "no command module exposes a repository seam at all"
