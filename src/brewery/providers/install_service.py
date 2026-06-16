"""Assemble and run the native install pipeline for a set of formulae."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable

import httpx

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.providers.downloader import Downloader
from brewery.providers.install_adapters import BrewAdapter, RepositoryCatalogAdapter
from brewery.providers.manifest import fetch_bottle_tab
from brewery.providers.orchestrator import InstallConfig, InstallReport, Orchestrator

# Async callable that invokes `brew <args>` and raises BrewCommandError on a
# non-zero exit — bind to your brew passthrough runner.
RunBrew = Callable[[list[str]], Awaitable[object]]


async def run_install(
    repo,
    names: list[str],
    *,
    run_brew: RunBrew,
    env: BreweryENV | None = None,
    install_concurrency: int = 1,
) -> InstallReport:
    """Install ``names`` via the native pipeline, brew-falling-back per formula.

    Args:
        repo: The Repository.
        names: Formula names to install (deps resolved from the catalog).
        run_brew: Async `brew <args>` runner for link/postinstall fallback.
        env: Brewery environment (paths), resolved if omitted.
        install_concurrency: Concurrent filesystem installs (downloads are
            always fully concurrent; default 1 serializes the install stages).

    Returns:
        The InstallReport (per-formula outcomes).
    """
    env = env or get_brewery_env()

    config = InstallConfig(
        prefix=env.prefix,
        repository=env.repository,
        api_path=str(env.api_path),  # <cache>/api/formula.jws.json
        staging_root=env.prefix / "var" / "homebrew" / ".staging",
    )

    async with httpx.AsyncClient() as client:
        downloader = Downloader(cache_dir=env.bottle_cache, client=client)
        tab_fetcher = functools.partial(fetch_bottle_tab, client)
        orchestrator = Orchestrator(
            catalog=RepositoryCatalogAdapter(repo),
            downloader=downloader,
            tab_fetcher=tab_fetcher,
            brew=BrewAdapter(repo.formula, run_brew),
            config=config,
            install_concurrency=install_concurrency,
        )

        return await orchestrator.install(names)
