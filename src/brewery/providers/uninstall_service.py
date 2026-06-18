"""Run the native uninstall pipeline for a set of formulae."""

from __future__ import annotations

import asyncio
import shutil

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.errors import BrewCommandError
from brewery.providers.linker import unlink_keg


async def run_uninstall(
    repo, names: list[str], *, env: BreweryENV | None = None
) -> None:
    """Unlink + remove each formula's kegs, brew-falling-back per formula.

    Args:
        repo: The Repository (for prefix/cellar paths).
        names: Canonical formula names to uninstall.
        run_brew: Async `brew <args>` runner for the fallback path.
        env: Brewery environment (paths), resolved if omitted.
    """
    env = env or get_brewery_env()
    for name in names:
        try:
            await asyncio.to_thread(
                _remove_formula, env.cellar / name, env.prefix, name
            )

        except OSError:
            try:
                await repo.formula.uninstall(names=[name])

            except BrewCommandError:
                pass  # repo._verify_removed reports the survivor as a failure


def _remove_formula(cellar_dir, prefix, name: str) -> None:
    """Unlink every installed keg of a formula, then delete its cellar dir.

    Args:
        cellar_dir: <prefix>/Cellar/<name>.
        prefix: The Homebrew prefix.
        name: The formula name.
    """
    if not cellar_dir.exists():
        return

    for keg in sorted(p for p in cellar_dir.iterdir() if p.is_dir()):
        unlink_keg(keg, prefix=prefix, name=name)  # realpath filter no-ops old kegs

    shutil.rmtree(cellar_dir)
