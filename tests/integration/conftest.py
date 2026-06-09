"""Fixtures for Brewery integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import orjson

if TYPE_CHECKING:
    from collections.abc import Generator
    from _layout import Brew
    from brewery.core.catalog import Catalog
    from brewery.core.config import BreweryENV
    from brewery.core.repo import Repository

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
        The fixture text data.
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
        The parsed JSON data.
    """
    return {k: orjson.loads(v) for k, v in fixture_text.items()}


@pytest.fixture
def fake_env(tmp_path, monkeypatch) -> BreweryENV:
    """Build a hermetic Homebrew prefix and populate the fixed yazi/act/iina
    layout that scan_installed needs, using the shared Brew builder.

    link_opt=False keeps the layout to bare kegs (no opt symlinks, no receipts,
    no link/pin bookkeeping), matching what these cache/repo tests expect.

    Args:
        tmp_path: The pytest tmp_path fixture.
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fake environment.
    """
    from _layout import Brew

    from brewery.core import config

    brew = Brew(tmp_path)
    brew.formula("yazi", "26.5.6", link_opt=False)
    brew.formula("act", "0.2.88", link_opt=False)
    brew.cask("iina", ["1.4.1,160"])

    env = brew.env
    monkeypatch.setattr(config, "_env_cache", env)

    return env


@pytest.fixture
def brew(tmp_path) -> Brew:
    """A fresh hermetic Homebrew layout built by the shared Brew helper.

    Args:
        tmp_path: The pytest tmp_path fixture.

    Returns:
        The fresh Brew layout.
    """
    from _layout import Brew

    return Brew(tmp_path)


@pytest.fixture
def empty_catalog(tmp_path) -> Generator[Catalog, None, None]:
    """A fresh Catalog backed by an isolated temp database file.

    Yields the open catalog and closes it on teardown, so tests need no manual
    try/finally close.

    Args:
        tmp_path: The pytest tmp_path fixture.

    Yields:
        The fresh Catalog.
    """
    from brewery.core.catalog import Catalog

    cat = Catalog(db_path=tmp_path / "catalog.db")
    yield cat
    cat.close()


@pytest.fixture
def catalog(fixture_json) -> Catalog:
    """Populate a Catalog from the formula/cask fixture JSON and return it.

    The DB lives in the test-isolated BREWERY_CACHE_DIR (set by the top-level
    conftest), so it never touches the real ~/.brewery/cache.

    Args:
        fixture_json: The fixture JSON fixture.

    Returns:
        The populated Catalog.
    """
    from brewery.core.catalog import Catalog

    formula_data: dict = fixture_json["formula"]
    cask_data: dict = fixture_json["cask"]

    cat = Catalog()

    formulae = [
        {
            "name": f["name"],
            "desc": f.get("desc"),
            "homepage": f.get("homepage"),
            "tap": f.get("tap"),
            "version": f["versions"]["stable"],
            "revision": f.get("revision", 0),
            "version_scheme": f.get("version_scheme", 0),
            "keg_only": int(f.get("keg_only", False)),
            "has_service": int(bool(f.get("service"))),
            "post_install": int(bool(f.get("post_install_caveat"))),
            "bottle_url": None,
            "bottle_sha256": None,
            "bottle_cellar": None,
            "bottle_rebuild": 0,
            "deprecated": int(f.get("deprecated", False)),
            "disabled": int(f.get("disabled", False)),
        }
        for f in formula_data["formulae"]
    ]

    deps = [
        {"pkg": f["name"], "dep": dep, "kind": "runtime"}
        for f in formula_data["formulae"]
        for dep in f.get("dependencies", [])
    ]

    aliases = [
        {"alias": a, "name": f["name"]}
        for f in formula_data["formulae"]
        for a in f.get("aliases", [])
    ]

    cat.write_formulae(formulae, deps, aliases)

    casks = [
        {
            "token": c["token"],
            "name": c["name"][0] if c.get("name") else None,
            "desc": c.get("desc"),
            "homepage": c.get("homepage"),
            "tap": c.get("tap"),
            "version": c.get("version"),
            "sha256": c.get("sha256"),
            "url": c.get("url"),
            "auto_updates": int(c.get("autobump", False)),
            "artifacts": orjson.dumps(c["artifacts"]).decode()
            if c.get("artifacts")
            else None,
            "depends_on": orjson.dumps(c["depends_on"]).decode()
            if c.get("depends_on")
            else None,
            "deprecated": int(c.get("deprecated", False)),
            "disabled": int(c.get("disabled", False)),
        }
        for c in cask_data["casks"]
    ]

    cat.write_casks(casks)

    return cat


@pytest.fixture
def mock_brew(monkeypatch, fixture_text, fake_env) -> list[tuple[str, ...]]:
    """Patch run_capture everywhere it is imported so subprocess boundaries
    never reach the real brew binary.  Returns the call log.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        fixture_text: The fixture text fixture.
        fake_env: The fake environment fixture.

    Returns:
        The call log.
    """
    calls: list[tuple[str, ...]] = []

    async def fake_run_capture(*cmd: str, timeout=None):
        cmd_t = tuple(cmd)
        calls.append(cmd_t)

        if cmd_t[:1] == ("du",):
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

    import brewery.core.shell as shell

    monkeypatch.setattr(shell, "run_capture", fake_run_capture)

    return calls


@pytest.fixture
def repo(mock_brew, catalog) -> Repository:
    """Repository wired to mock subprocesses and a pre-populated catalog.

    Args:
        mock_brew: The mock subprocess call log.
        catalog: The pre-populated catalog.
    """
    from brewery.core.repo import Repository

    return Repository(catalog=catalog)
