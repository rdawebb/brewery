"""Assemble and run the native install pipeline for a set of formulae."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable

import httpx

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.providers.downloader import Downloader
from brewery.providers.install_adapters import BrewAdapter, RepositoryCatalogAdapter
from brewery.providers.manifest import fetch_bottle_tab
from brewery.providers.orchestrator import (
    InstallConfig,
    InstallReport,
    Orchestrator,
    ProgressPort,
)

RunBrew = Callable[[list[str]], Awaitable[object]]


def build_orchestrator(
    repo,
    *,
    client: httpx.AsyncClient,
    env: BreweryENV,
    run_brew: RunBrew,
    progress: ProgressPort | None = None,
) -> Orchestrator:
    """Assemble an Orchestrator bound to an open client and the repo's ports.

    Shared by the install and upgrade services.

    Args:
        repo: The Repository providing catalog/cache/formula-backend ports.
        client: An open httpx.AsyncClient.
        env: Brewery environment (paths).
        run_brew: Async `brew <args>` runner for link/postinstall fallback.
        progress: Optional progress sink forwarded to the Orchestrator.

    Returns:
        A configured Orchestrator.
    """
    config = InstallConfig(
        prefix=env.prefix,
        repository=env.repository,
        api_path=str(env.api_path),  # <cache>/api/formula.jws.json
        staging_root=env.prefix / "var" / "homebrew" / ".staging",
    )

    return Orchestrator(
        catalog=RepositoryCatalogAdapter(repo),
        downloader=Downloader(cache_dir=env.bottle_cache, client=client),
        tab_fetcher=functools.partial(fetch_bottle_tab, client),
        brew=BrewAdapter(repo.formula, run_brew),
        config=config,
        progress=progress,
    )


async def run_install(
    repo,
    names: list[str],
    *,
    run_brew: RunBrew,
    env: BreweryENV | None = None,
    progress: ProgressPort | None = None,
) -> InstallReport:
    """Install `names` via the native pipeline, brew-falling-back per formula.

    Args:
        repo: The Repository.
        names: Formula names to install (deps resolved from the catalog).
        run_brew: Async `brew <args>` runner for link/postinstall fallback.
        env: Brewery environment, resolved if omitted.
        progress: Optional progress sink forwarded to the Orchestrator.

    Returns:
        The InstallReport (per-formula outcomes).
    """
    env = env or get_brewery_env()

    async with httpx.AsyncClient() as client:
        orchestrator = build_orchestrator(
            repo,
            client=client,
            env=env,
            run_brew=run_brew,
            progress=progress,
        )

        return await orchestrator.install(names)
