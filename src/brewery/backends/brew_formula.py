"""Module for managing Homebrew formulae."""

from __future__ import annotations

from typing import List

from .base import Package
from .brew_common import brew_json


async def list_installed_formulae() -> List[Package]:
    """List all installed Homebrew formulae.

    Returns:
        List[Package]: A list of installed packages.
    """
    data = await brew_json(["info", "--json=v2", "--installed"])
    formulae = data.get("formulae", [])
    output: List[Package] = []

    for f in formulae:
        name = f.get("name", "?")
        version = f.get("installed", [{}])[-1].get("version", f.get("versions", {}).get("stable", "?"))
        desc = f.get("desc")
        installed = f.get("installed", [])
        installed_at = installed[-1].get("installed_time") if installed else None
        keg_only = f.get("keg_only", False)
        outdated = f.get("outdated", False)
        pinned = f.get("pinned", False)
        status = [s for s, flag in [("keg-only", keg_only), ("outdated", outdated), ("pinned", pinned)] if flag]
        size_bytes = f.get("installed", [{}])[-1].get("poured_from_bottle", 0)
        size_human = "-" # Placeholder

        output.append(
            Package(
                name=name,
                version=str(version),
                desc=desc,
                installed_at=str(installed_at) if installed_at else None,
                status=status,
                size_human=size_human,
                pkg_type="formula"
        ))

    return output