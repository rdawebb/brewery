"""Theme configuration for the application."""

from textual.app import App


def set_theme(app: App) -> None:
    """Set the theme for the application.
    
    Args:
        app (App): The Textual application instance.
    """
    app.styles.background = "black"
    app.styles.color = "white"