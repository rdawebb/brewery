"""Run the native uninstall pipeline for a set of formulae."""

from __future__ import annotations

import asyncio

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.errors import BrewCommandError, OperationInProgressError
from brewery.core.logging import BreweryLogger, get_logger
from brewery.providers.base import UninstallBackend
from brewery.providers.cellar import remove_rack

log: BreweryLogger = get_logger(name=__name__)


async def run_uninstall(
    names: list[str],
    *,
    formula: UninstallBackend,
    env: BreweryENV | None = None,
) -> None:
    """Unlink + remove each formula's kegs, brew-falling-back per formula.

    Args:
        names: Canonical formula names to uninstall.
        formula: Formula backend for the per-formula brew fallback.
        env: Brewery environment (paths), resolved if omitted.
    """
    env = env or get_brewery_env()
    for name in names:
        try:
            await asyncio.to_thread(remove_rack, env.cellar / name, env.prefix, name)

        except OperationInProgressError as exc:
            # brew locks the same rack, so falling back to it would fail too;
            # the caller's removal verification reports the survivor as a failure
            log.warning(event="uninstall_rack_locked", formula=name, error=str(exc))

        except OSError:
            try:
                await formula.uninstall(names=[name])

            except BrewCommandError:
                pass  # verification reports the survivor as a failure
