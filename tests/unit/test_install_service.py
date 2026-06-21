"""Unit tests for the install assembly function."""

from __future__ import annotations

import functools

import pytest
from _stubs import MockClient, MockRepo, _run_brew

import brewery.providers.install_service as svc
from brewery.providers.install_adapters import BrewAdapter, RepositoryCatalogAdapter
from brewery.providers.orchestrator import InstallConfig

pytestmark = pytest.mark.asyncio


class MockDownloader:
    """Downloader stub that records the most-recently constructed instance."""

    last = None

    def __init__(self, cache_dir, client) -> None:
        """Initialise the mock downloader and record this instance.

        Args:
            cache_dir: The cache directory passed by the service.
            client: The HTTP client passed by the service.
        """
        MockDownloader.last = self
        self.cache_dir = cache_dir
        self.client = client


class MockOrchestrator:
    """Orchestrator stub that records construction kwargs and install calls."""

    last = None

    def __init__(self, **kwargs) -> None:
        """Initialise the mock orchestrator and record this instance.

        Args:
            **kwargs: The keyword arguments passed by the service layer.
        """
        MockOrchestrator.last = self
        self.kwargs = kwargs
        self.installed_with = None

    async def install(self, names) -> str:
        """Record the names and return a sentinel report string.

        Args:
            names: The list of formula names to install.

        Returns:
            A sentinel string `"report:<comma-joined names>"`.
        """
        self.installed_with = names
        return f"report:{','.join(names)}"


@pytest.fixture
def patched(monkeypatch) -> MockClient:
    """Patch httpx.AsyncClient, Downloader, and Orchestrator with stubs.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The MockClient instance that the patched AsyncClient constructor returns.
    """
    client = MockClient()
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda: client)
    monkeypatch.setattr(svc, "Downloader", MockDownloader)
    monkeypatch.setattr(svc, "Orchestrator", MockOrchestrator)

    return client


async def test_returns_orchestrator_report(patched, mock_env) -> None:
    """Test that run_install returns the report produced by the orchestrator."""
    repo = MockRepo()
    report = await svc.run_install(
        repo, ["wget", "curl"], run_brew=_run_brew, env=mock_env
    )
    assert report == "report:wget,curl"
    assert MockOrchestrator.last is not None
    assert MockOrchestrator.last.installed_with == ["wget", "curl"]


async def test_client_is_closed(patched, mock_env) -> None:
    """Test that the HTTP client is closed after run_install completes."""
    repo = MockRepo()
    await svc.run_install(repo, ["wget"], run_brew=_run_brew, env=mock_env)
    assert patched.closed is True


async def test_downloader_built_with_env_cache_and_client(patched, mock_env) -> None:
    """Test that the Downloader is constructed with the env bottle_cache and the live client."""
    repo = MockRepo()
    await svc.run_install(repo, ["wget"], run_brew=_run_brew, env=mock_env)
    assert MockDownloader.last is not None
    dl = MockDownloader.last
    assert dl.cache_dir == mock_env.bottle_cache
    assert dl.client is patched


async def test_orchestrator_wired_with_adapters_and_config(patched, mock_env) -> None:
    """Test that the Orchestrator receives correctly wired adapters and InstallConfig."""
    repo = MockRepo()
    await svc.run_install(
        repo, ["wget"], run_brew=_run_brew, env=mock_env, install_concurrency=3
    )
    assert MockOrchestrator.last is not None
    kw = MockOrchestrator.last.kwargs

    # Catalog port is the repo-backed adapter
    assert isinstance(kw["catalog"], RepositoryCatalogAdapter)
    assert kw["catalog"]._repo is repo

    # Brew port wraps the formula backend + the injected runner
    assert isinstance(kw["brew"], BrewAdapter)
    assert kw["brew"]._backend is repo.formula
    assert kw["brew"]._run_brew is _run_brew

    # Tab fetcher is fetch_bottle_tab bound to the live client
    tf = kw["tab_fetcher"]
    assert isinstance(tf, functools.partial)
    assert tf.func is svc.fetch_bottle_tab
    assert tf.args == (patched,)

    # Downloader and concurrency forwarded
    assert kw["downloader"] is MockDownloader.last
    assert kw["install_concurrency"] == 3

    # Config derived from env
    cfg = kw["config"]
    assert isinstance(cfg, InstallConfig)
    assert cfg.prefix == mock_env.prefix
    assert cfg.repository == mock_env.repository
    assert cfg.api_path == str(mock_env.api_path)
    assert cfg.staging_root == mock_env.prefix / "var" / "homebrew" / ".staging"


async def test_env_resolved_when_omitted(patched, mock_env, monkeypatch) -> None:
    """Test that omitting env= falls back to get_brewery_env() automatically."""
    monkeypatch.setattr(svc, "get_brewery_env", lambda: mock_env)
    repo = MockRepo()
    await svc.run_install(repo, ["wget"], run_brew=_run_brew)  # no env=
    assert MockOrchestrator.last is not None
    cfg = MockOrchestrator.last.kwargs["config"]
    assert cfg.prefix == mock_env.prefix
