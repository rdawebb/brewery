"""Dependency queries over already-merged installed packages."""

from __future__ import annotations

from brewery.core.models import Package, PackageKind


def blocking_dependents(
    packages: list[Package], removal: set[str]
) -> dict[str, list[str]]:
    """Installed formulae outside `removal` that still require a target.

    Reads each target's receipt-derived reverse-deps and drops any dependent
    that is itself being removed in the same batch.

    Args:
        packages: Installed packages to search; non-formulae are ignored.
        removal: Canonical formula names slated for removal.

    Returns:
        target -> sorted installed formulae that require it (empty if none).
    """
    if not removal:
        return {}

    installed = {p.name: p for p in packages if p.kind == PackageKind.FORMULA}

    blockers: dict[str, list[str]] = {}
    for name in removal:
        pkg = installed.get(name)
        if pkg is None:
            continue

        deps = sorted(d for d in pkg.used_by if d not in removal)
        if deps:
            blockers[name] = deps

    return blockers
