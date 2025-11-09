"""Key mappings for the Brewery application."""

from textual.app import App


def bind_keys(app: App) -> None:
    """Bind key mappings for the Brewery application.

    Args:
        app (App): The Brewery application instance.
    """
    app.bind("q", "quit")
    app.bind("/", "focus('filters.search')")
    app.bind("f", "focus('filters')")
    app.bind("l", "focus('logs')")
    app.bind("enter", "app.refresh_selected")
    app.bind("r", "app.refresh_all")