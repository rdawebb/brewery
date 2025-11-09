"""Widgets for displaying logs in the application."""

from __future__ import annotations

from textual.widgets import Log


class LogsPanel(Log):
    """Panel to display application logs."""

    def on_mount(self) -> None:
        """Called when the logs panel is mounted."""
        self.highlight = True
        self.wrap = False
        self.write("Logs ready...\n")