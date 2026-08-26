"""Shared test configuration and fixtures for Brewery.

Redirects all on-disk state (cache, logs, config) into a temp dir so tests never
touch the user's real directories, and resets the module-level singletons/caches
between tests so that test order cannot leak state.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _layout import Brew
    from _mocks import MockHTTPClient

    from brewery.core.catalog import Catalog
    from brewery.core.config import BreweryENV
    from brewery.core.repo import Repository

# Isolates on disk state at import time, before any brewery module is imported
assert not [m for m in sys.modules if m.startswith("brewery")], (
    "a brewery module was imported before the test directories were redirected; "
    "move the import that pulled it in inside the fixture that needs it"
)

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="brewery-tests-"))
os.environ["BREWERY_CACHE_DIR"] = str(_TMP_ROOT / "cache")
os.environ["BREWERY_LOG_DIR"] = str(_TMP_ROOT / "logs")
os.environ["BREWERY_CONFIG_HOME"] = str(_TMP_ROOT / "config")


# Resets module-level state between tests to avoid state leakage (only already-imported modules)
_RESETTABLE: list[tuple[str, str, object]] = [
    ("brewery.core.config", "_env_cache", None),
    # Renderer width-cache load flag + dict.
    ("brewery.cli.renderers", "_width_cache_loaded", False),
]


def pytest_collection_modifyitems(config, items) -> None:
    """Mark every test by the directory it lives in, unless it says otherwise.

    Args:
        config: The pytest config (unused).
        items: The collected test items, marked in place.
    """
    layers = ("unit", "integration", "cli")
    for item in items:
        if any(item.get_closest_marker(layer) for layer in layers):
            continue

        parts = item.path.parts
        for layer in layers:
            if layer in parts:
                item.add_marker(getattr(pytest.mark, layer))
                break


@pytest.fixture(autouse=True)
def _reset_module_state() -> Generator[None, None, None]:
    """Reset known singletons/caches before each test."""
    for modname, attr, value in _RESETTABLE:
        mod = sys.modules.get(modname)
        if mod is not None and hasattr(mod, attr):
            setattr(mod, attr, value)

    # Clear renderer width cache in place if present
    renderers = sys.modules.get("brewery.cli.renderers")
    if renderers is not None and hasattr(renderers, "_width_cache"):
        renderers._width_cache.clear()

    # Clear the on-disk file cache and any written settings
    import shutil

    for var in ("BREWERY_CACHE_DIR", "BREWERY_CONFIG_HOME"):
        root = Path(os.environ[var])
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)

    yield


@pytest.fixture
def delays(monkeypatch) -> list[float]:
    """Record every retry backoff instead of sleeping through it.

    `core.retry`, `core.decorators` and the downloader all reach their backoff
    through the one `asyncio` module object, so patching it here covers every
    retry path at once, and the recorded delays let a test assert the policy
    rather than only that it ran quickly.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The delays that were asked for, in order.
    """
    recorded: list[float] = []

    async def _record(delay: float = 0.0, *_args, **_kwargs) -> None:
        """Record the requested delay and return immediately.

        Args:
            delay: The delay that would have been slept.
            *_args: Anything else asyncio.sleep accepts, ignored.
            **_kwargs: Anything else asyncio.sleep accepts, ignored.
        """
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _record)

    return recorded


def _build_keg(version_dir: Path) -> Path:
    """Populate a minimal openssl@3-shaped keg at version_dir and return it.

    Args:
        version_dir: The directory to populate as a keg version root.

    Returns:
        The populated version directory.
    """
    (version_dir / "bin").mkdir(parents=True)
    (version_dir / "lib").mkdir()

    exe = version_dir / "bin" / "openssl"
    exe.write_bytes(b"MACHO-binary")
    os.chmod(exe, 0o555)

    lib = version_dir / "lib" / "libssl.dylib"
    lib.write_bytes(b"lib")
    os.chmod(lib, 0o444)

    os.symlink("libssl.dylib", version_dir / "lib" / "libssl.3.dylib")
    (version_dir / ".brew").mkdir()
    (version_dir / ".brew" / "openssl@3.rb").write_bytes(b"class Openssl3\nend\n")

    return version_dir


@pytest.fixture
def staged_keg(tmp_path) -> Path:
    """A staged openssl@3 3.0 keg tree ready for installation or relocation.

    Args:
        tmp_path: The pytest-provided temporary directory.

    Returns:
        The path to the populated keg version directory.
    """
    return _build_keg(tmp_path / "stage" / "openssl@3" / "3.0")


@pytest.fixture
def build_keg() -> Callable[[Path], Path]:
    """Return the keg-builder function for tests that need more than one keg.

    Returns:
        The _build_keg callable, for constructing additional kegs in a test.
    """
    return _build_keg


@pytest.fixture
def mock_env(tmp_path, monkeypatch) -> BreweryENV:
    """A hermetic BreweryENV backed by tmp_path with no real filesystem layout.

    Patches the module-level `_env_cache` singleton so any code path that
    calls `get_brewery_env()` without an explicit `env=` argument gets this
    instance.  Integration tests override this fixture in their own conftest to
    add a pre-populated keg layout.

    Returns:
        A BreweryENV instance.
    """
    from brewery.core import config
    from brewery.core.config import BreweryENV

    prefix = tmp_path / "homebrew"
    cache = tmp_path / "cache"
    env = BreweryENV(
        prefix=prefix,
        cellar=prefix / "Cellar",
        caskroom=prefix / "Caskroom",
        repository=prefix / "Library" / "Homebrew",
        api_path=cache / "api" / "formula.jws.json",
        bottle_cache=cache,
        cache=tmp_path / "brewery_cache",
    )
    monkeypatch.setattr(config, "_env_cache", env)

    return env


@pytest.fixture
def http_client():
    """Factory for a MockHTTPClient.

    Returns a callable so each test builds its own client with the response
    shape it needs.

    Returns:
        A callable taking the same arguments as MockHTTPClient.
    """
    from _mocks import MockHTTPClient

    def _make(response=None, *, raise_on_get=None) -> MockHTTPClient:
        """Create a MockHTTPClient with the given response and raise_on_get.

        Args:
            response: The response to return from the client.
            raise_on_get: If set, raise an exception when the client is used.

        Returns:
            A MockHTTPClient instance.
        """
        return MockHTTPClient(response, raise_on_get=raise_on_get)

    return _make


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixture_text() -> dict[str, str]:
    """Load all fixture files as strings.

    Returns:
        The fixture text data.
    """
    return {
        name: (FIXTURE_DIR / f"{name}.json").read_text()
        for name in ("formula", "cask", "outdated")
    }


@pytest.fixture
def fixture_json(fixture_text) -> dict[str, dict]:
    """Parse all fixture text as JSON.

    Args:
        fixture_text: The fixture text fixture.

    Returns:
        The parsed JSON data.
    """
    import orjson

    return {k: orjson.loads(v) for k, v in fixture_text.items()}


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
def catalog(fixture_json) -> Catalog:
    """Populate a Catalog from the formula/cask fixture JSON and return it.

    The DB lives in the test-isolated BREWERY_CACHE_DIR (set above), so it
    never touches the real cache.

    Args:
        fixture_json: The fixture JSON fixture.

    Returns:
        The populated Catalog.
    """
    import orjson

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
def mock_brew(monkeypatch, fixture_text, mock_env) -> list[tuple[str, ...]]:
    """Patch run_brew in the brew provider so subprocess boundaries never reach
    the real brew binary.  Returns the call log as ("brew", *args) tuples.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        fixture_text: The fixture text fixture.
        mock_env: The mock environment fixture.

    Returns:
        The call log.
    """
    calls: list[tuple[str, ...]] = []

    from brewery.core.shell import BrewResult

    async def mock_run_brew(args: list[str], *, output, check):
        calls.append(("brew", *args))
        return BrewResult(stdout="", stderr="", returncode=0)

    import brewery.providers.brew as brew_mod

    monkeypatch.setattr(brew_mod, "run_brew", mock_run_brew)

    return calls


@pytest.fixture
def repo(mock_brew, catalog) -> Repository:
    """Repository wired to mock subprocesses and a pre-populated catalog.

    Args:
        mock_brew: The mock subprocess call log.
        catalog: The pre-populated catalog.

    Returns:
        A Repository instance.
    """
    from brewery.core.repo import Repository

    return Repository(catalog=catalog)
