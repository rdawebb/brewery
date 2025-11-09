"""Core application state management for package data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List

from textual.app import App

from .models import PackageRow
from brewery.backends.brew_formula import list_installed_formulae
from brewery.backends.brew_cask import list_installed_casks


@dataclass
class Filters:
    """Data class to hold filter settings."""

    query: str = ""
    type: str = "all"  # Options: "all", "formula", "cask"
    outdated: bool = False
    pinned: bool = False
    notlinked: bool = False


class AppStore:
    """Class to manage the application state."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.packages: List[PackageRow] = []
        self.filters = Filters()

    async def initial_load(self) -> None:
        """Load initial data into the store."""
        formulae, casks = await asyncio.gather(
            list_installed_formulae(),
            list_installed_casks()
        )
        packages = formulae + casks
        rows: List[PackageRow] = []

        for p in packages:
            rows.append(
                PackageRow(
                    key=f"{p.pkg_type}:{p.name}",
                    name=p.name,
                    type=p.pkg_type,
                    version=p.version,
                    status=",".join(p.status) if p.status else "ok",
                    size_human=p.size_human,
                    installed_at=p.installed_at,
                )
            )

        self.rows = rows
        table = self.app.query_one("#table")
        table.load_rows(self.filtered_rows())

    def filtered_rows(self) -> List[PackageRow]:
        """Get the list of packages filtered by the current filter settings.

        Returns:
            List[PackageRow]: The filtered list of package rows.
        """
        rows = self.rows
        f = self.filters

        if f.type != "all":
            rows = [r for r in rows if r.type == f.type]
        if f.query:
            q = f.query.lower()
            rows = [r for r in rows if q in r.name.lower()]
        
        return sorted(rows, key=lambda r: r.name.lower())