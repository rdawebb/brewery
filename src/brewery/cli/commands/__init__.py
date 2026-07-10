"""Command modules for the Brewery CLI."""

from __future__ import annotations

# Import command modules for their registration side effects
from brewery.cli.commands import (  # noqa: F401  (registration side effects)
    cleanup,
    install,
    outdated,
    query,
    uninstall,
    upgrade,
)
from brewery.cli.commands.config import config_app
from brewery.cli.commands.daemon import daemon_app
from brewery.cli.context import app

app.add_typer(
    daemon_app,
    name="daemon",
    aliases=["d"],
    help="Manage the Brewery background refresh daemon.",
)
app.add_typer(
    config_app,
    name="config",
    aliases=["cfg"],
    help="View and manage brewery configuration.",
)
