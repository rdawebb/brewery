"""Link and unlink installed formulae, degrading to a failure note per formula."""

from __future__ import annotations

from pathlib import Path

from brewery.core.config import BreweryENV
from brewery.core.errors import LinkError
from brewery.core.fs_state import is_effectively_linked, linked_names
from brewery.core.models import Package, PackageStatus
from brewery.providers.linker import LinkResult, UnlinkResult, link_keg, unlink_keg

# Conflicting paths quoted back to the user before the list is elided
_MAX_QUOTED_CONFLICTS = 3

Notes = list[tuple[str, str]]  # (name, reason) advisories or failures
LinkOutcome = tuple[list[tuple[str, LinkResult]], Notes, Notes]
UnlinkOutcome = tuple[list[tuple[str, UnlinkResult]], Notes, Notes]


def _already_linked(pkg: Package, env: BreweryENV) -> bool:
    """Report whether a formula is currently linked into the prefix.

    Read from brew's bookkeeping directory, falling back to probing the prefix
    when no such directory exists.

    Args:
        pkg: The installed formula.
        env: Brewery environment (paths).

    Returns:
        True if the formula appears linked.
    """
    linked: set[str] | None = linked_names(prefix=env.prefix)
    if linked is None:
        return is_effectively_linked(name=pkg.name, env=env)

    return pkg.name in linked


def _conflict_reason(name: str, error: LinkError) -> str:
    """Summarise a link conflict as a one-line failure reason.

    Args:
        name: The formula that failed to link.
        error: The raised conflict error.

    Returns:
        A reason naming the conflicting paths and the command that resolves them.
    """
    paths: list[str] = [dst for dst, _ in error.conflicts[:_MAX_QUOTED_CONFLICTS]]
    elided: int = len(error.conflicts) - len(paths)
    listing: str = ", ".join(paths) + (f" (+{elided} more)" if elided else "")

    return f"conflicts with {listing} - re-run 'brewery link --overwrite {name}'"


def run_link(
    pkgs: list[Package],
    *,
    env: BreweryENV,
    overwrite: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> LinkOutcome:
    """Link each formula into the prefix, collecting advisories and failures.

    Args:
        pkgs: The installed formulae to link.
        env: Brewery environment (paths).
        overwrite: Delete conflicting prefix files while linking.
        force: Allow keg-only formulae to be linked.
        dry_run: Report what would be linked without touching the filesystem.

    Returns:
        Linked (name, result) pairs, advisory (name, reason) pairs, and failing
        (name, reason) pairs.
    """
    linked: list[tuple[str, LinkResult]] = []
    advisories: Notes = []
    failures: Notes = []

    for pkg in pkgs:
        if _already_linked(pkg=pkg, env=env):
            advisories.append(
                (
                    pkg.name,
                    f"already linked - run 'brewery unlink {pkg.name}' to relink",
                )
            )
            continue

        if PackageStatus.KEG_ONLY in pkg.status and not force:
            # A dry run still previews what --force would link
            advisories.append((pkg.name, "keg-only - link it with --force"))
            if not dry_run:
                continue

        try:
            result: LinkResult = link_keg(
                _keg(pkg),
                prefix=env.prefix,
                name=pkg.name,
                overwrite=overwrite,
                dry_run=dry_run,
            )

        except LinkError as e:
            failures.append((pkg.name, _conflict_reason(name=pkg.name, error=e)))

        except OSError as e:
            failures.append((pkg.name, str(object=e)))

        else:
            linked.append((pkg.name, result))

    return linked, advisories, failures


def run_unlink(
    pkgs: list[Package], *, env: BreweryENV, dry_run: bool = False
) -> UnlinkOutcome:
    """Unlink each formula from the prefix.

    Keg-only formulae are not special-cased: they own no prefix symlinks,
    so unlinking them removes nothing and succeeds.

    Args:
        pkgs: The installed formulae to unlink.
        env: Brewery environment (paths).
        dry_run: Report what would be removed without touching the filesystem.

    Returns:
        Unlinked (name, result) pairs, advisory (name, reason) pairs, and failing
        (name, reason) pairs.
    """
    unlinked: list[tuple[str, UnlinkResult]] = []
    advisories: Notes = []
    failures: Notes = []

    for pkg in pkgs:
        try:
            result: UnlinkResult = unlink_keg(
                _keg(pkg), prefix=env.prefix, name=pkg.name, dry_run=dry_run
            )

        except OSError as e:
            failures.append((pkg.name, str(object=e)))

        else:
            unlinked.append((pkg.name, result))

    return unlinked, advisories, failures


def _keg(pkg: Package) -> Path:
    """Resolve a package's active keg directory.

    Args:
        pkg: The installed formula, whose `path` is its active keg.

    Returns:
        The keg directory.

    Raises:
        ValueError: The record carries no keg path.
    """
    if not pkg.path:
        raise ValueError(f"{pkg.name} has no keg path")

    return Path(pkg.path)
