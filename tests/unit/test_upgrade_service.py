"""Unit tests for the upgrade assembly function."""

from __future__ import annotations

from pathlib import Path

import pytest
from _stubs import MockClient, MockRepo, _run_brew, patch_httpx

import brewery.providers.upgrade_service as svc
from brewery.providers.orchestrator import InstallReport, Outcome


class MockOrchestrator:
    """Records the upgrade call and returns a sentinel report."""

    last: MockOrchestrator | None = None

    def __init__(self) -> None:
        """Initialise with no upgraded state."""
        MockOrchestrator.last = self
        self.upgraded_with: tuple | None = None
        self.report: InstallReport | None = None

    async def upgrade(self, names, old_kegs) -> InstallReport:
        """Record the upgrade call and return a sentinel report.

        Args:
            names: The names of the packages to upgrade.
            old_kegs: The old kegs to upgrade from.

        Returns:
            A report marking every name natively upgraded.
        """
        self.upgraded_with = (names, old_kegs)
        self.report = InstallReport(outcomes={n: Outcome.NATIVE for n in names})

        return self.report


@pytest.fixture
def patched(monkeypatch) -> tuple[MockClient, dict]:
    """Patch httpx and the shared build_orchestrator with recorders.

    Returns:
        A tuple of the mock client and the built dictionary.
    """
    client = patch_httpx(monkeypatch)

    built: dict = {}

    def _build(repo, *, client, env, run_brew, progress=None) -> MockOrchestrator:
        """Record the build call and return a mock orchestrator.

        Args:
            repo: The repository to build.
            client: The HTTP client to use.
            env: The environment variables to use.
            run_brew: The brew run command to use.
            progress: The optional progress sink.

        Returns:
            A mock orchestrator.
        """
        built.update(
            repo=repo,
            client=client,
            env=env,
            run_brew=run_brew,
            progress=progress,
        )

        return MockOrchestrator()

    monkeypatch.setattr(svc, "build_orchestrator", _build)

    return client, built


async def test_returns_orchestrator_report(patched, mock_env) -> None:
    """Test that run_upgrade returns the orchestrator's report and forwards names + old_kegs."""
    old = {"wget": Path("/p/Cellar/wget/1.0"), "curl": Path("/p/Cellar/curl/8.0")}
    report = await svc.run_upgrade(
        MockRepo(), ["wget", "curl"], old, run_brew=_run_brew, env=mock_env
    )
    assert MockOrchestrator.last is not None
    assert MockOrchestrator.last.upgraded_with == (["wget", "curl"], old)
    assert report is MockOrchestrator.last.report  # Passed through untouched


async def test_client_is_closed(patched, mock_env) -> None:
    """Test that the HTTP client is closed after run_upgrade completes."""
    client, _ = patched
    await svc.run_upgrade(MockRepo(), ["wget"], {}, run_brew=_run_brew, env=mock_env)
    assert client.closed is True


async def test_client_gets_the_streaming_timeout(patched, mock_env) -> None:
    """Test that upgrade builds its client with the same timeout install uses."""
    client, _ = patched
    await svc.run_upgrade(MockRepo(), ["wget"], {}, run_brew=_run_brew, env=mock_env)
    assert client.kwargs["timeout"] is svc.PIPELINE_TIMEOUT


async def test_build_orchestrator_receives_client_env_and_runner(
    patched, mock_env
) -> None:
    """Test that the shared assembler gets the open client, env, and runner."""
    client, built = patched
    repo = MockRepo()
    await svc.run_upgrade(repo, ["wget"], {}, run_brew=_run_brew, env=mock_env)
    assert built["repo"] is repo
    assert built["client"] is client
    assert built["env"] is mock_env
    assert built["run_brew"] is _run_brew


async def test_env_resolved_when_omitted(patched, mock_env, monkeypatch) -> None:
    """Test that omitting env= falls back to get_brewery_env()."""
    monkeypatch.setattr(svc, "get_brewery_env", lambda: mock_env)
    _, built = patched
    await svc.run_upgrade(MockRepo(), ["wget"], {}, run_brew=_run_brew)  # No env=
    assert built["env"] is mock_env
