"""Unit tests for the install/upgrade pipeline assembly functions."""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
from _stubs import MockClient, MockPorts, _run_brew, patch_httpx

import brewery.providers.pipeline as svc
from brewery.providers.install_adapters import BrewAdapter, CatalogAdapter
from brewery.providers.orchestrator import InstallConfig, InstallReport, Outcome

pytestmark = pytest.mark.asyncio


class MockDownloader:
    """Downloader stub that records the most-recently constructed instance."""

    last = None

    def __init__(self, cache_dir, client) -> None:
        """Initialise the mock downloader and record this instance.

        Args:
            cache_dir: The cache directory passed by the pipeline.
            client: The HTTP client passed by the pipeline.
        """
        MockDownloader.last = self
        self.cache_dir = cache_dir
        self.client = client


class MockOrchestrator:
    """Orchestrator stub that records construction kwargs and install/upgrade calls."""

    last: MockOrchestrator | None = None

    def __init__(self, **kwargs) -> None:
        """Initialise the mock orchestrator and record this instance.

        Args:
            **kwargs: The keyword arguments passed by the pipeline.
        """
        MockOrchestrator.last = self
        self.kwargs = kwargs
        self.installed_with: list[str] | None = None
        self.upgraded_with: tuple | None = None
        self.report: InstallReport | None = None

    async def install(self, names) -> InstallReport:
        """Record the names and return a sentinel report.

        Args:
            names: The list of formula names to install.

        Returns:
            A report marking every name natively installed.
        """
        self.installed_with = names
        self.report = InstallReport(outcomes={n: Outcome.NATIVE for n in names})

        return self.report

    async def upgrade(self, names, old_kegs) -> InstallReport:
        """Record the upgrade call and return a sentinel report.

        Args:
            names: The names of the formulae to upgrade.
            old_kegs: The old kegs to upgrade from.

        Returns:
            A report marking every name natively upgraded.
        """
        self.upgraded_with = (names, old_kegs)
        self.report = InstallReport(outcomes={n: Outcome.NATIVE for n in names})

        return self.report


@pytest.fixture
def patched(monkeypatch) -> MockClient:
    """Patch httpx.AsyncClient, Downloader, and Orchestrator with stubs.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The MockClient instance that the patched AsyncClient constructor returns.
    """
    client = patch_httpx(monkeypatch)

    monkeypatch.setattr(svc, "Downloader", MockDownloader)
    monkeypatch.setattr(svc, "Orchestrator", MockOrchestrator)

    return client


@pytest.fixture
def ports() -> MockPorts:
    """The catalog/cache_mgr/formula sentinels every pipeline call needs.

    Returns:
        A fresh MockPorts instance.
    """
    return MockPorts()


def _install(names, ports, **kwargs):
    """Call run_install with the sentinel ports spread out.

    Args:
        names: Formula names to install.
        ports: The MockPorts sentinels.
        **kwargs: Extra keyword arguments forwarded to run_install.

    Returns:
        The run_install coroutine.
    """
    return svc.run_install(
        names,
        catalog=ports.catalog,
        cache_mgr=ports.cache_mgr,
        formula=ports.formula,
        run_brew=_run_brew,
        **kwargs,
    )


def _upgrade(names, old_kegs, ports, **kwargs):
    """Call run_upgrade with the sentinel ports spread out.

    Args:
        names: Formula names to upgrade.
        old_kegs: The current active kegs, keyed by name.
        ports: The MockPorts sentinels.
        **kwargs: Extra keyword arguments forwarded to run_upgrade.

    Returns:
        The run_upgrade coroutine.
    """
    return svc.run_upgrade(
        names,
        old_kegs,
        catalog=ports.catalog,
        cache_mgr=ports.cache_mgr,
        formula=ports.formula,
        run_brew=_run_brew,
        **kwargs,
    )


class TestRunInstall:
    """Tests for run_install."""

    async def test_returns_orchestrator_report(self, patched, ports, mock_env) -> None:
        """Test that run_install returns the report produced by the orchestrator."""
        report = await _install(["wget", "curl"], ports, env=mock_env)
        assert MockOrchestrator.last is not None
        assert MockOrchestrator.last.installed_with == ["wget", "curl"]
        assert report is MockOrchestrator.last.report  # Passed through untouched

    async def test_client_is_closed(self, patched, ports, mock_env) -> None:
        """Test that the HTTP client is closed after run_install completes."""
        await _install(["wget"], ports, env=mock_env)
        assert patched.closed is True

    async def test_client_gets_the_streaming_timeout(
        self, patched, ports, mock_env
    ) -> None:
        """Test the client is built with the pipeline timeout, not httpx's 5s default."""
        await _install(["wget"], ports, env=mock_env)
        assert patched.kwargs["timeout"] is svc.PIPELINE_TIMEOUT

        # Pinned: a bottle body is streamed, so the read budget is a stall budget
        assert svc.PIPELINE_TIMEOUT.read == 30.0
        assert svc.PIPELINE_TIMEOUT.connect == 10.0

    async def test_downloader_built_with_env_cache_and_client(
        self, patched, ports, mock_env
    ) -> None:
        """Test the Downloader is built with the env bottle_cache and the live client."""
        await _install(["wget"], ports, env=mock_env)
        assert MockDownloader.last is not None
        dl = MockDownloader.last
        assert dl.cache_dir == mock_env.bottle_cache
        assert dl.client is patched

    async def test_orchestrator_wired_with_adapters_and_config(
        self, patched, ports, mock_env
    ) -> None:
        """Test the Orchestrator receives wired adapters and an env-derived config."""
        await _install(["wget"], ports, env=mock_env)
        assert MockOrchestrator.last is not None
        kw = MockOrchestrator.last.kwargs

        # Catalog port is the adapter over the catalog + installed-state cache
        assert isinstance(kw["catalog"], CatalogAdapter)
        assert kw["catalog"]._catalog is ports.catalog
        assert kw["catalog"]._cache_mgr is ports.cache_mgr

        # Brew port wraps the formula backend + the injected runner
        assert isinstance(kw["brew"], BrewAdapter)
        assert kw["brew"]._backend is ports.formula
        assert kw["brew"]._run_brew is _run_brew

        # Tab fetcher is fetch_bottle_tab bound to the live client
        tf = kw["tab_fetcher"]
        assert isinstance(tf, functools.partial)
        assert tf.func is svc.fetch_bottle_tab
        assert tf.args == (patched,)

        # Downloader forwarded; concurrency left to the Orchestrator's default
        assert kw["downloader"] is MockDownloader.last
        assert "install_concurrency" not in kw

        # Config derived from env
        cfg = kw["config"]
        assert isinstance(cfg, InstallConfig)
        assert cfg.prefix == mock_env.prefix
        assert cfg.repository == mock_env.repository
        assert cfg.api_path == str(mock_env.api_path)
        assert cfg.staging_root == mock_env.prefix / "var" / "homebrew" / ".staging"

    async def test_env_resolved_when_omitted(
        self, patched, ports, mock_env, monkeypatch
    ) -> None:
        """Test that omitting env= falls back to get_brewery_env() automatically."""
        monkeypatch.setattr(svc, "get_brewery_env", lambda: mock_env)
        await _install(["wget"], ports)  # no env=
        assert MockOrchestrator.last is not None
        cfg = MockOrchestrator.last.kwargs["config"]
        assert cfg.prefix == mock_env.prefix


class TestRunUpgrade:
    """Tests for run_upgrade."""

    async def test_returns_orchestrator_report(self, patched, ports, mock_env) -> None:
        """Test run_upgrade returns the orchestrator's report and forwards old_kegs."""
        old = {"wget": Path("/p/Cellar/wget/1.0"), "curl": Path("/p/Cellar/curl/8.0")}
        report = await _upgrade(["wget", "curl"], old, ports, env=mock_env)
        assert MockOrchestrator.last is not None
        assert MockOrchestrator.last.upgraded_with == (["wget", "curl"], old)
        assert report is MockOrchestrator.last.report  # Passed through untouched

    async def test_client_is_closed(self, patched, ports, mock_env) -> None:
        """Test that the HTTP client is closed after run_upgrade completes."""
        await _upgrade(["wget"], {}, ports, env=mock_env)
        assert patched.closed is True

    async def test_client_gets_the_streaming_timeout(
        self, patched, ports, mock_env
    ) -> None:
        """Test that upgrade builds its client with the same timeout install uses."""
        await _upgrade(["wget"], {}, ports, env=mock_env)
        assert patched.kwargs["timeout"] is svc.PIPELINE_TIMEOUT

    async def test_orchestrator_receives_the_same_ports_as_install(
        self, patched, ports, mock_env
    ) -> None:
        """Test that upgrade wires the identical port set install does."""
        await _upgrade(["wget"], {}, ports, env=mock_env)
        assert MockOrchestrator.last is not None
        kw = MockOrchestrator.last.kwargs
        assert kw["catalog"]._catalog is ports.catalog
        assert kw["catalog"]._cache_mgr is ports.cache_mgr
        assert kw["brew"]._backend is ports.formula
        assert kw["downloader"].client is patched

    async def test_env_resolved_when_omitted(
        self, patched, ports, mock_env, monkeypatch
    ) -> None:
        """Test that omitting env= falls back to get_brewery_env()."""
        monkeypatch.setattr(svc, "get_brewery_env", lambda: mock_env)
        await _upgrade(["wget"], {}, ports)  # No env=
        assert MockOrchestrator.last is not None
        cfg = MockOrchestrator.last.kwargs["config"]
        assert cfg.prefix == mock_env.prefix
