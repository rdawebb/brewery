"""Details panel widget for displaying package information."""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Static


class DetailsPanel(Vertical):
    """Panel to show details of the selected package."""

    def on_mount(self) -> None:
        """Called when the panel is mounted."""
        self.title = Static("Details", classes="details_title")
        self.body = Static("Select a package to see details", classes="details_body")
        self.mount(self.title)
        self.mount(self.body)

    def show_details(self, details: str) -> None:
        """Update the panel to show package details.

        Args:
            details (str): The details of the selected package.
        """
        self.body.update(details)