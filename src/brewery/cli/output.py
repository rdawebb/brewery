"""Console output primitives shared by the CLI commands."""

from __future__ import annotations

import atexit
import getpass
import os
import sys
import termios
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from functools import partial
from typing import TYPE_CHECKING

from rich.prompt import Confirm
from rich.status import Status
from rich.text import Text
from rich.theme import Theme

from brewery.cli.context import console
from brewery.core.sudo import askpass

if TYPE_CHECKING:
    from brewery.cli.progress import ProgressReporter
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


@contextmanager
def _tty_restored_at_exit() -> Generator[None]:
    """Put the terminal's settings back at exit if a prompt is cut short.

    A second Ctrl-C mid-prompt cuts `askpass`'s shutdown short, and its thread
    dies in `getpass` with echo still off.

    Yields:
        None, with the restore registered until the block ends.
    """
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)

    except OSError:
        yield  # No tty: getpass falls back to stdin, nothing to restore
        return

    try:
        restore = partial(
            termios.tcsetattr, fd, termios.TCSAFLUSH, termios.tcgetattr(fd)
        )
        atexit.register(restore)

        try:
            yield

        finally:
            atexit.unregister(restore)

    finally:
        os.close(fd)


def sudo_ask(
    pause: Callable[[], AbstractContextManager[object]] = nullcontext,
) -> Callable[[str], str | None]:
    """Build `askpass`'s `ask`, which reads sudo's password on the terminal.

    Args:
        pause: Takes the live display off screen while the prompt is up.

    Returns:
        A callable taking sudo's prompt and returning the password, or None
        with no terminal or on Ctrl-D.
    """

    def ask(prompt: str) -> str | None:
        if not console.is_terminal:
            return None

        with pause(), _tty_restored_at_exit():
            try:
                return getpass.getpass(prompt)

            except EOFError:
                return None

    return ask


@contextmanager
def _stopped(status: Status) -> Generator[None]:
    """Stop a spinner for the block, and start it again after.

    Args:
        status: The spinner to take off screen.

    Yields:
        None, with the spinner stopped; it is started again on exit, even if
        the block raises.
    """
    status.stop()
    try:
        yield

    finally:
        status.start()


def sudo_prompts(
    display: ProgressReporter | Status | None,
) -> AbstractContextManager[None]:
    """Answer sudo's prompts on the terminal, with `display` off screen meanwhile.

    Args:
        display: The live display to pause, or None when there isn't one.

    Returns:
        `askpass` set up with `sudo_ask`, for the length of a `with` block.
    """
    if display is None:
        pause: Callable[[], AbstractContextManager[object]] = nullcontext

    elif isinstance(display, Status):
        pause = partial(_stopped, display)

    else:
        pause = display.paused

    return askpass(sudo_ask(pause))


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
