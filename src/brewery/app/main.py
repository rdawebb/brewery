"""Main application module for Brewery."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Header, Footer

from brewery.core.store import AppStore
from .widgets.package_table import PackageTable
from .widgets.filters_panel import FiltersPanel
from .widgets.details_panel import DetailsPanel
from .widgets.logs_panel import LogsPanel
from .keymap import bind_keys
from .theme import set_theme


class Brewery(App):
    """Main application class for Brewery."""

    CSS_PATH = None
    BINDINGS = []

    loading: reactive[bool] = reactive(False)

    def __init__(self) -> None:
        super().__init__()
        self.store = AppStore(self)

    def on_mount(self) -> None:
        """Called when the app is mounted."""
        set_theme(self)
        bind_keys(self)
        self.call_after_refresh(self.store.initial_load)

    def compose(self) -> ComposeResult:
        """Compose the UI layout.
        
        Returns:
            ComposeResult: The composed UI elements.
        """
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield FiltersPanel(id="filters")
            with Vertical(id="center"):
                yield PackageTable(id="table")
                yield LogsPanel(id="logs")
            yield DetailsPanel(id="details")
        yield Footer()

def run() -> None:
    """Run the Brewery application."""
    Brewery().run()

if __name__ == "__main__":
    run()