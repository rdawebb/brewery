"""Pin and unpin formulae at their active keg."""

from __future__ import annotations

from pathlib import Path

from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.decorators import log_operation
from brewery.core.models import Notes
from brewery.core.repo import Repository
from brewery.providers.pinning import pin, unpin
from brewery.services.resolve import installed_formulae


@log_operation(event_prefix="pin_packages", log_args=["names"])
def pin_packages(
    repo: Repository, names: list[str], *, env: BreweryENV | None = None
) -> tuple[list[str], Notes, Notes]:
    """Pin formulae at their active keg, preventing upgrades.

    Args:
        repo: The data facade to read installed state through.
        names: Name(s) of the formulae to pin.
        env: Brewery environment (paths), resolved if omitted.

    Returns:
        Tuple of (pinned names, (name, reason) advisories, (name, reason) failures).
    """
    env = env or repo.cache_mgr.env or get_brewery_env()
    pkgs, failures = installed_formulae(repo, names)

    pinned: list[str] = []
    advisories: Notes = []
    for pkg in pkgs:
        if not pkg.path:
            failures.append((pkg.name, "no active keg"))

        elif pin(prefix=env.prefix, name=pkg.name, keg=Path(pkg.path)):
            pinned.append(pkg.name)

        else:
            advisories.append((pkg.name, "already pinned"))

    if pinned:
        repo.cache_mgr.invalidate()

    return pinned, advisories, failures


@log_operation(event_prefix="unpin_packages", log_args=["names"])
def unpin_packages(
    repo: Repository, names: list[str], *, env: BreweryENV | None = None
) -> tuple[list[str], Notes, Notes]:
    """Unpin formulae, allowing them to be upgraded again.

    Args:
        repo: The data facade to read installed state through.
        names: Name(s) of the formulae to unpin.
        env: Brewery environment (paths), resolved if omitted.

    Returns:
        Tuple of (unpinned names, (name, reason) advisories, (name, reason) failures).
    """
    env = env or repo.cache_mgr.env or get_brewery_env()
    pkgs, failures = installed_formulae(repo, names)

    unpinned: list[str] = []
    advisories: Notes = []
    for pkg in pkgs:
        if unpin(prefix=env.prefix, name=pkg.name):
            unpinned.append(pkg.name)

        else:
            advisories.append((pkg.name, "not pinned"))

    if unpinned:
        repo.cache_mgr.invalidate()

    return unpinned, advisories, failures
