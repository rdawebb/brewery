"""Resolve user-supplied names to installed formulae."""

from __future__ import annotations

from brewery.core.models import Notes, Package, PackageKind
from brewery.core.repo import Repository


def installed_formulae(
    repo: Repository, names: list[str]
) -> tuple[list[Package], Notes]:
    """Resolve user-supplied names to installed formulae.

    Args:
        repo: The data facade to read installed state and aliases through.
        names: Name(s) or alias(es) of the formulae.

    Returns:
        The resolved packages, and (name, reason) pairs for those not installed.
    """
    found: list[Package] = []
    failures: Notes = []

    for name in names:
        pkg: Package | None = repo.cache_mgr.find_installed(
            repo.catalog.resolve_alias(name), PackageKind.FORMULA
        )
        if pkg is None:
            failures.append((name, "not installed"))

        else:
            found.append(pkg)

    return found, failures
