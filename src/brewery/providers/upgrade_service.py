"""Assemble and run the native upgrade pipeline for a set of formulae."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.providers.install_service import build_orchestrator
from brewery.providers.orchestrator import InstallReport

RunBrew = Callable[[list[str]], Awaitable[object]]


async def run_upgrade(
    repo,
    names: list[str],
    old_kegs: dict[str, Path],
    *,
    run_brew: RunBrew,
    env: BreweryENV | None = None,
    install_concurrency: int = 1,
) -> InstallReport:
    """Upgrade `names` via the native pipeline, brew-falling-back per formula.

    Args:
        repo: The Repository.
        names: Formula names to upgrade (already resolved to outdated targets).
        old_kegs: Each target's current active keg, to unlink and stamp as replaced.
        run_brew: Async `brew <args>` runner for link/postinstall fallback.
        env: Brewery environment, resolved if omitted.
        install_concurrency: Concurrent filesystem installs.

    Returns:
        The InstallReport (per-formula outcomes).
    """
    env = env or get_brewery_env()
    async with httpx.AsyncClient() as client:
        orch = build_orchestrator(
            repo,
            client=client,
            env=env,
            run_brew=run_brew,
            install_concurrency=install_concurrency,
        )
        return await orch.upgrade(names, old_kegs)
