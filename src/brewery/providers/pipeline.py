"""Assemble and run the native bottle pipeline for a set of formulae."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.providers.base import PackageBackend
from brewery.providers.downloader import Downloader
from brewery.providers.install_adapters import (
    BrewAdapter,
    CatalogAdapter,
    CatalogSource,
    InstalledSource,
)
from brewery.providers.manifest import fetch_bottle_tab
from brewery.providers.orchestrator import (
    InstallConfig,
    InstallReport,
    Orchestrator,
    ProgressPort,
)

RunBrew = Callable[[list[str]], Awaitable[object]]

# httpx's 5s default is too tight for streaming a bottle body; read/write
# timeouts are per-socket-operation, not whole-request, so 30s bounds a stall
# without capping how long a large bottle may take
PIPELINE_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def build_orchestrator(
    *,
    catalog: CatalogSource,
    cache_mgr: InstalledSource,
    formula: PackageBackend,
    client: httpx.AsyncClient,
    env: BreweryENV,
    run_brew: RunBrew,
    progress: ProgressPort | None = None,
) -> Orchestrator:
    """Assemble an Orchestrator bound to an open client and the given ports.

    Shared by `run_install` and `run_upgrade`.

    Args:
        catalog: The catalog backing formula/alias/dependency lookups.
        cache_mgr: Installed-state cache, used to answer `is_satisfied`.
        formula: Formula backend for the per-formula brew fallback.
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
        catalog=CatalogAdapter(catalog=catalog, cache_mgr=cache_mgr),
        downloader=Downloader(cache_dir=env.bottle_cache, client=client),
        tab_fetcher=functools.partial(fetch_bottle_tab, client),
        brew=BrewAdapter(formula, run_brew),
        config=config,
        progress=progress,
    )


async def run_install(
    names: list[str],
    *,
    catalog: CatalogSource,
    cache_mgr: InstalledSource,
    formula: PackageBackend,
    run_brew: RunBrew,
    env: BreweryENV | None = None,
    progress: ProgressPort | None = None,
) -> InstallReport:
    """Install `names` via the native pipeline, brew-falling-back per formula.

    Args:
        names: Formula names to install (deps resolved from the catalog).
        catalog: The catalog backing formula/alias/dependency lookups.
        cache_mgr: Installed-state cache, used to answer `is_satisfied`.
        formula: Formula backend for the per-formula brew fallback.
        run_brew: Async `brew <args>` runner for link/postinstall fallback.
        env: Brewery environment, resolved if omitted.
        progress: Optional progress sink forwarded to the Orchestrator.

    Returns:
        The InstallReport (per-formula outcomes).
    """
    env = env or get_brewery_env()

    async with httpx.AsyncClient(timeout=PIPELINE_TIMEOUT) as client:
        orchestrator = build_orchestrator(
            catalog=catalog,
            cache_mgr=cache_mgr,
            formula=formula,
            client=client,
            env=env,
            run_brew=run_brew,
            progress=progress,
        )

        return await orchestrator.install(names)


async def run_upgrade(
    names: list[str],
    old_kegs: dict[str, Path],
    *,
    catalog: CatalogSource,
    cache_mgr: InstalledSource,
    formula: PackageBackend,
    run_brew: RunBrew,
    env: BreweryENV | None = None,
    progress: ProgressPort | None = None,
) -> InstallReport:
    """Upgrade `names` via the native pipeline, brew-falling-back per formula.

    Args:
        names: Formula names to upgrade (already resolved to outdated targets).
        old_kegs: Each target's current active keg, to unlink and stamp as replaced.
        catalog: The catalog backing formula/alias/dependency lookups.
        cache_mgr: Installed-state cache, used to answer `is_satisfied`.
        formula: Formula backend for the per-formula brew fallback.
        run_brew: Async `brew <args>` runner for link/postinstall fallback.
        env: Brewery environment, resolved if omitted.
        progress: Optional progress sink forwarded to the Orchestrator.

    Returns:
        The InstallReport (per-formula outcomes).
    """
    env = env or get_brewery_env()

    async with httpx.AsyncClient(timeout=PIPELINE_TIMEOUT) as client:
        orch = build_orchestrator(
            catalog=catalog,
            cache_mgr=cache_mgr,
            formula=formula,
            client=client,
            env=env,
            run_brew=run_brew,
            progress=progress,
        )

        return await orch.upgrade(names, old_kegs)
