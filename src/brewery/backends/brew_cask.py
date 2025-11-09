"""Module for managing Homebrew Cask packages."""

from __future__ import annotations

import asyncio
from typing import List

from .base import Package
from .brew_common import brew_json


async def list_installed_casks() -> List[Package]:
    """List all installed Homebrew Casks.

    Returns:
        List[Package]: A list of installed cask packages.
    """
    process = await asyncio.create_subprocess_exec(
        "brew", "list", "--cask",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await process.communicate()
    if process.returncode != 0:
        return []
    
    names = [n.strip() for n in out.decode().splitlines() if n.strip()]
    if not names:
        return []
    
    data = await brew_json(["info", "--json=v2", *names])
    casks = data.get("casks", [])
    output: List[Package] = []

    for c in casks:
        name = c.get("name", [c.get("token", "?")])[0]
        version = c.get("version", "?")

        output.append(
            Package(
                name=str(name),
                version=str(version),
                desc=None,
                installed_at=None,
                size_human="-",
                status=[],
                pkg_type="cask",
            )
        )

    return output