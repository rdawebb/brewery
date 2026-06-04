"""Fixtures for Brewery integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import orjson

if TYPE_CHECKING:
    from brewery.core.config import BreweryENV

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def _load(name: str) -> str:
    """Load a fixture file by name.

    Args:
        name: The fixture file name.

    Returns:
        The contents of the fixture file as a string.
    """
    return (FIXTURE_DIR / name).read_text()


@pytest.fixture
def fixture_text() -> dict[str, str]:
    """Load all fixture files as strings.

    Returns:
        A dictionary mapping fixture names to their contents as strings.
    """
    return {
        "formula": _load("formula.json"),
        "cask": _load("cask.json"),
        "outdated": _load("outdated.json"),
    }


@pytest.fixture
def fixture_json(fixture_text) -> dict[str, dict]:
    """Parse all fixture text as JSON.

    Args:
        fixture_text: The fixture text fixture.

    Returns:
        A dictionary mapping fixture names to their parsed JSON contents.
    """
    return {k: orjson.loads(v) for k, v in fixture_text.items()}


@pytest.fixture
def fake_env(monkeypatch) -> BreweryENV:
    """Pin the Homebrew prefix so path construction is deterministic and does
    no subprocess discovery.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The pinned BreweryENV instance.
    """
    from brewery.core import config
    from brewery.core.config import BreweryENV

    env = BreweryENV(
        prefix=Path("/opt/homebrew"),
        cellar=Path("/opt/homebrew/Cellar"),
        caskroom=Path("/opt/homebrew/Caskroom"),
    )
    monkeypatch.setattr(config, "_env_cache", env)

    return env


@pytest.fixture
def mock_brew(monkeypatch, fixture_text, fake_env):
    """Patch run_capture everywhere it is imported so the real run_json parses
    fixture text. Returns the dispatcher so tests can inspect call history.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        fixture_text: The fixture text to parse.
        fake_env: The fake BreweryENV instance.

    Returns:
        The dispatcher instance.
    """
    calls: list[tuple[str, ...]] = []

    async def fake_run_capture(*cmd: str, timeout=None):
        cmd_t = tuple(cmd)
        calls.append(cmd_t)

        if cmd_t[:1] == ("du",):
            # Size lookup stdout is "<kb>\t<path>"
            return (f"4096\t{cmd_t[-1]}", "", 0)
        if "--caskroom" in cmd_t:
            return ("/opt/homebrew/Caskroom", "", 0)
        if cmd_t[:2] == ("brew", "outdated"):
            return (fixture_text["outdated"], "", 0)
        if "info" in cmd_t:
            if "--cask" in cmd_t:
                return (fixture_text["cask"], "", 0)
            return (fixture_text["formula"], "", 0)
        return ("", "", 0)

    # Binding each module to the fake run_capture
    import brewery.core.shell as shell
    import brewery.providers.brew_cask as brew_cask
    import brewery.providers.package_builder as package_builder

    monkeypatch.setattr(shell, "run_capture", fake_run_capture)
    monkeypatch.setattr(package_builder, "run_capture", fake_run_capture)
    monkeypatch.setattr(brew_cask, "run_capture", fake_run_capture)

    return calls
