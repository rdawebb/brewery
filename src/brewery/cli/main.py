"""CLI entry point for Brewery package management tool."""

from __future__ import annotations

import os
import sys

from typer_extensions import ExtendedTyper

# Importing the commands package registers every command and sub-app onto `app`
import brewery.cli.commands  # noqa: F401  (registration side effects)
from brewery.cli.context import app, console
from brewery.core.errors import EXIT_SYSTEM_ERROR, BrewCommandError
from brewery.core.logging import BreweryLogger, configure_logging, get_logger
from brewery.core.shell import BrewOutput, run_brew

log: BreweryLogger = get_logger(name=__name__)


def _derive_known_commands(app: ExtendedTyper = app) -> set[str]:
    """Collect every command name, sub-app name, and alias registered on the app.

    Args:
        typer_app: The app to introspect (defaults to the brewery CLI app).

    Returns:
        The set of known command names and aliases.
    """
    names: set[str] = set()
    for info in app.registered_commands:
        name = info.name or getattr(info.callback, "__name__", None)
        if name:
            names.add(name)

    for group in app.registered_groups:
        if group.name:
            names.add(group.name)

    for aliases in app.list_commands_with_aliases().values():
        names.update(aliases)

    return names


KNOWN_COMMANDS: set[str] = _derive_known_commands()


def _brew_passthrough(argv: list[str]) -> int:
    """Forward an unknown brewery command straight to brew.

    Args:
        argv: The command and arguments to pass to brew.

    Returns:
        The exit code of the brew command.
    """
    import asyncio

    # main() exits here before app(), so the setup callback never configures logging
    configure_logging(console_level=os.environ.get("BREWERY_LOG_CONSOLE"))

    try:
        returncode = asyncio.run(
            run_brew(argv, output=BrewOutput.INHERIT, check=False, timeout=None)
        ).returncode

    # check=False, so the only failure left is brew missing from PATH
    except BrewCommandError:
        console.print("\n✗ brew not found on PATH\n", style="bold red")
        returncode = EXIT_SYSTEM_ERROR

    except KeyboardInterrupt:
        returncode = 130

    log.info(event="brew_passthrough", argv=" ".join(argv), returncode=returncode)

    return returncode


def main(argv: list[str] | None = None) -> None:
    """Intercepts the entry point for the brewery CLI to handle commands passthrough.

    Args:
        argv: The command-line arguments to pass to the brewery CLI.
    """
    if argv is None:
        argv = sys.argv[1:]

    # Pass unknown and non-flag arguments straight to brew
    if argv and not argv[0].startswith("-") and argv[0] not in KNOWN_COMMANDS:
        sys.exit(_brew_passthrough(argv))

    app()


if __name__ == "__main__":
    main()
