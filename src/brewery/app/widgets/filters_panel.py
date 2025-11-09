"""Widget for the filters panel in the Brewery application."""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Checkbox, Label, Input, Select


class FiltersPanel(Vertical):
    """Panel for filtering packages."""

    def compose(self):
        """Compose the filters panel UI."""
        yield Label("Filters", id="filters_title")
        yield Input(placeholder="Search packages...", id="search_input")
        yield Select((
            ("All", "all"),
            ("Formulae", "formula"),
            ("Casks", "cask"),
        ), id="filters_type")
        yield Checkbox("Outdated", id="filters_outdated")
        yield Checkbox("Pinned", id="filters_pinned")
        yield Checkbox("Not linked", id="filters_notlinked")
