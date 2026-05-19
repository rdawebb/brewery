"""Fetch outdated packages directly from Homebrew."""

from __future__ import annotations

import time
from typing import Any

from brewery.core.errors import BrewCommandError
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.shell import run_json

log: BreweryLogger = get_logger(name=__name__)


def _enrich_entry(entry: dict, kind: str) -> dict:
    """Enrich a package entry with required Package fields.

    Args:
        entry: The raw package entry from 'brew outdated' JSON.
        kind: The package kind ("formula" or "cask").

    Returns:
        The enriched entry with required fields.
    """
    entry["kind"] = kind
    entry["versions"] = entry.get("installed_versions", [])
    entry["status"] = 1  # PackageStatus.OUTDATED
    entry["metadata"] = {"latest_version": entry.get("current_version")}
    return entry


async def fetch_outdated() -> list[dict]:
    """Fetch outdated packages directly from Homebrew.

    Returns:
        Combined list of outdated formula and cask packages.

    Raises:
        BrewCommandError: If the Homebrew command fails.
    """
    try:
        start: float = time.perf_counter()
        log.info(event="outdated_fetch_start")

        data: Any = await run_json("brew", "outdated", "--json=v2")

        entries: list[dict] = [
            *[_enrich_entry(entry=f, kind="formula") for f in data.get("formulae", [])],
            *[_enrich_entry(entry=c, kind="cask") for c in data.get("casks", [])],
        ]

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="outdated_fetch_complete", count=len(entries), duration_ms=duration_ms
        )

        return entries

    except BrewCommandError as e:
        log.error(event="outdated_fetch_failed", error=str(object=e))
        return []
