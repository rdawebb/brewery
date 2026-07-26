"""Console output primitives shared by the CLI commands."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from rich.prompt import Confirm
from rich.text import Text
from rich.theme import Theme

from brewery.cli.context import console

if TYPE_CHECKING:
    from rich.status import Status

    from brewery.core.models import Package

_CONFIRM_BULLET = "•"

# DECSC / DECRC: save the cursor before the prompt, restore it and erase to the end
# of the screen once answered
_SAVE_CURSOR = "\x1b7"
_RESTORE_AND_ERASE = "\x1b8\x1b[J"


def spinner(message: str, *, style: str = "yellow") -> Status:
    """Build the standard status spinner for a long-running command stage.

    Args:
        message: Text shown beside the spinner.
        style: Rich colour applied to the message.

    Returns:
        A rich `Status` context manager.
    """
    return console.status(
        status=f"[bold {style}]{message}[/bold {style}]", refresh_per_second=5
    )


def confirm_or_cancel(
    prompt: str,
    *,
    yes: bool,
    default: bool = True,
    style: str = "bold green",
    cancel_msg: str = "• Cancelled\n",
) -> bool:
    """Ask for confirmation, printing a cancellation notice when declined.

    Args:
        prompt: The question to put to the user.
        yes: When true, skip the prompt and proceed.
        default: The answer applied when the user just presses enter.
        style: Rich colour for the prompt line, e.g. yellow for destructive commands.
        cancel_msg: Notice printed when the user declines.

    Returns:
        True if the command should proceed.
    """
    sys.stdout.write("\n")  # The blank line above the prompt; outlives the erase below

    if yes:
        return True

    erase = console.is_terminal
    if erase:
        console.file.write(_SAVE_CURSOR)
        console.file.flush()

    prompt_theme = Theme({"prompt.choices": style, "prompt.default": style})

    try:
        with console.use_theme(prompt_theme):
            confirmed: bool = Confirm.ask(
                Text(f"{_CONFIRM_BULLET} {prompt}", style=style),
                console=console,
                default=default,
            )

    except EOFError:
        confirmed = False

    if erase:
        console.file.write(_RESTORE_AND_ERASE)
        console.file.flush()

    else:
        # No terminal echoed the answer's newline, so close the line
        sys.stdout.write("\n")

    if confirmed:
        return True

    console.print(cancel_msg, style="dim")

    return False


def pkg_line(pkg: Package) -> str:
    """Format a package as 'name version' for a result list.

    Args:
        pkg: The package to format.

    Returns:
        The package name followed by its active version, if any.
    """
    return f"{pkg.name} {pkg.versions[0] if pkg.versions else ''}"


def print_result(
    header: str,
    lines: Iterable[str],
    *,
    style: str,
    bullet: str = "-",
    line_style: str | None = None,
) -> None:
    """Print a result header followed by an indented bullet list.

    The header is printed verbatim, so callers control their own blank-line
    spacing. The header prints even when `lines` is empty (a zero count is still
    a result worth showing).

    Args:
        header: The already-formatted header line, e.g. "✓ Installed 2 package(s)\n".
        lines: The body lines, one bullet each.
        style: Rich style for the header.
        bullet: The bullet glyph, dimmed unless `line_style` is set.
        line_style: Rich style applied to each whole body line.
    """
    console.print(header, style=style)

    for line in lines:
        if line_style:
            console.print(f"  {bullet} {line}", style=line_style)

        else:
            console.print(f"  [dim]{bullet}[/dim] {line}")


def print_failures(header: str, failures: Sequence[tuple[str, str]]) -> None:
    """Print a failure header and its (name, reason) pairs. No-op when empty.

    Args:
        header: The already-formatted header line.
        failures: Pairs of failing item name and the reason it failed.
    """
    if not failures:
        return

    console.print(header, style="bold red")

    for name, reason in failures:
        console.print(f"  [dim]-[/dim] {name} - {reason}")


def print_advisories(advisories: Sequence[tuple[str, str]]) -> None:
    """Print advisory (name, reason) pairs. No-op when empty.

    Advisories are conditions brew warns about but does not fail on, e.g. pinning
    an already-pinned formula.

    Args:
        advisories: Pairs of item name and the advice about it.
    """
    for name, reason in advisories:
        console.print(f"⚠ {name} - {reason}", style="yellow")
