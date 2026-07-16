"""Rich-backed progress reporter for the native install/upgrade pipeline."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from rich.console import Console
from rich.filesize import decimal
from rich.progress import (
    BarColumn,
    Column,
    Progress,
    ProgressColumn,
    TaskID,
    TextColumn,
)
from rich.spinner import Spinner
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.progress import Task

# Outcome values (Outcome.<...>.value) that mean the package did not install
_FAILED_OUTCOMES = frozenset({"failed", "skipped_dep_failed"})

_REVEAL_DELAY = 0.5  # Seconds before the live display appears

# Cap the visible rows to the download/installation concurrency
_MAX_VISIBLE = 8

_NAME_WIDTH = 22  # Formula-name column; truncates with an ellipsis rather than wraps
_STATUS_WIDTH = 20  # Holds "↓ 999.9 MB/999.9 MB"; keeps the activity column aligned

# Per-row glyphs tracking a package through the pipeline
_GLYPH_DOWNLOAD = "↓"  # Actively downloading
_GLYPH_DOWNLOADED = "[grey50]•[/grey50]"  # Downloaded, waiting to install
_GLYPH_INSTALL = "[green]•[/green]"  # Actively installing
_GLYPH_DONE = "[green]✓[/green]"  # Successfully installed
_GLYPH_FAILED = "[red]✗[/red]"  # Failed


class _ActivityColumn(ProgressColumn):
    """Trailing indicator cell: a determinate bar when the total is known,
    an animated spinner when it is not, and nothing once the row has finished.

    A single column keeps the indicator in one aligned position across every row,
    delegates the bar to an internal `BarColumn` so Rich's rendering is reused.
    """

    def __init__(
        self, *, bar_width: int | None = None, spinner_name: str = "dots"
    ) -> None:
        """Initialise the activity column.

        Args:
            bar_width: Fixed bar width, or None to flex into the remaining space.
            spinner_name: The `rich.spinner` name used while a total is unknown.
        """
        super().__init__()
        self._bar = BarColumn(
            bar_width=bar_width, complete_style="green", finished_style="green"
        )
        self._spinner = Spinner(spinner_name)

    def render(self, task: Task) -> RenderableType:
        """Render the indicator for `task`, keyed off its `activity` field.

        Args:
            task: The Rich task to render.

        Returns:
            A determinate green bar for `bar`, an animated spinner for
            `spinner`, or a blank cell (finished row) otherwise.
        """
        activity = task.fields.get("activity")
        if activity == "bar":  # Download/overall anchor bar
            return self._bar.render(task)

        if activity == "spinner":  # Install, or download with no length
            return self._spinner.render(task.get_time())

        return Text("")  # Finished row: only the glyph is shown


def make_reporter(console: Console) -> ProgressReporter | None:
    """Return a reporter, or None when a live display would be inappropriate.

    Progress is suppressed when the output is not a TTY (piped or redirected),
    where spinner/redraw control codes would corrupt the captured output.

    Args:
        console: The CLI console the reporter should render through.

    Returns:
        A ProgressReporter, or None to run the pipeline silently.
    """
    if not console.is_terminal:
        return None

    return ProgressReporter(console)


class ProgressReporter:
    """A `ProgressPort` implementation driving a `rich.progress.Progress`."""

    def __init__(self, console: Console) -> None:
        """Initialise the reporter (does not start the live display).

        Args:
            console: The console to render the progress display through.
        """
        self._progress = Progress(
            TextColumn(
                "  {task.fields[glyph]}", table_column=Column(width=3, no_wrap=True)
            ),
            TextColumn(
                "{task.fields[name]}",
                table_column=Column(
                    width=_NAME_WIDTH, no_wrap=True, overflow="ellipsis"
                ),
            ),
            TextColumn(
                "{task.fields[status]}",
                style="dim",
                table_column=Column(
                    width=_STATUS_WIDTH, no_wrap=True, overflow="ellipsis"
                ),
            ),
            _ActivityColumn(bar_width=None),
            console=console,
            transient=True,
            refresh_per_second=10,
        )
        self._overall: TaskID | None = None
        self._total = 0
        self._done = 0
        self._tasks: dict[str, TaskID] = {}
        self._finished: deque[TaskID] = deque()  # finished rows, oldest first
        self._reveal_handle: asyncio.TimerHandle | None = None
        self._started = False

    def begin(self, total: int) -> None:
        """Add the overall bar and arm the deferred reveal.

        The overall anchor bar is only displayed for multi-package transactions;
        for a single formula it is omitted.

        Args:
            total: The number of packages that will be installed.
        """
        if total > 1:
            self._total = total
            self._overall = self._progress.add_task(
                "",
                total=total,
                name="[bold green]Installing[/bold green]",
                status=f"0/{total}",
                glyph=" ",
                activity="bar",
            )

        loop = asyncio.get_running_loop()
        self._reveal_handle = loop.call_later(_REVEAL_DELAY, self._reveal)

    def _reveal(self) -> None:
        """Start the live display (called by the deferred-reveal timer)."""
        self._reveal_handle = None
        if not self._started:
            self._progress.start()
            self._started = True

    def _task_for(self, name: str) -> TaskID:
        """Return `name`'s task, creating it on first sight.

        Args:
            name: The formula name.

        Returns:
            The Rich task id tracking this formula.
        """
        tid = self._tasks.get(name)
        if tid is None:
            tid = self._progress.add_task(
                "", total=None, name=name, status="", glyph=" ", activity="spinner"
            )
            self._tasks[name] = tid
            self._trim()

        return tid

    def update(
        self,
        name: str,
        stage: str,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        """Update the row for `name` at the given stage.

        Args:
            name: The formula name.
            stage: "download" (with byte counts) or "install".
            done: Downloaded bytes so far (download stage only).
            total: Total bytes, if the server reported a Content-Length.
        """
        tid = self._task_for(name)
        if stage == "download":
            got = decimal(done) if done is not None else "?"
            size = f"/{decimal(total)}" if total is not None else ""
            downloaded = total is not None and done is not None and done >= total

            self._progress.update(
                tid,
                glyph=_GLYPH_DOWNLOADED if downloaded else _GLYPH_DOWNLOAD,
                status=f"{got}{size}",
                total=total,
                completed=done or 0,
                activity="bar" if total is not None else "spinner",
            )

        elif stage == "install":
            self._progress.update(
                tid, glyph=_GLYPH_INSTALL, status="installing", activity="spinner"
            )

    def finish(self, name: str, outcome: str) -> None:
        """Mark `name` done: freeze its row with a glyph, advance the overall bar.

        Args:
            name: The formula name.
            outcome: The terminal `Outcome` value.
        """
        failed = outcome in _FAILED_OUTCOMES
        glyph = _GLYPH_FAILED if failed else _GLYPH_DONE
        tid = self._tasks.get(name)
        if tid is not None:
            # activity="" blanks the indicator, stop_task freezes the row
            self._progress.update(tid, glyph=glyph, status="", activity="")
            self._progress.stop_task(tid)
            self._finished.append(tid)
            self._trim()

        if self._overall is not None:
            self._done += 1
            self._progress.update(
                self._overall, advance=1, status=f"{self._done}/{self._total}"
            )

    def _trim(self) -> None:
        """Drop the oldest finished rows so the region fits the screen.

        Keeps the visible package rows at or below `_MAX_VISIBLE`, removing
        only finished rows -- oldest first -- so the live region never scrolls
        and the transient stop can clear it fully.
        """
        anchor = 1 if self._overall is not None else 0
        while len(self._progress.tasks) - anchor > _MAX_VISIBLE and self._finished:
            self._progress.remove_task(self._finished.popleft())

    def end(self) -> None:
        """Cancel a pending reveal and stop the live display (clears it)."""
        if self._reveal_handle is not None:
            self._reveal_handle.cancel()
            self._reveal_handle = None

        if self._started:
            self._progress.stop()
            self._started = False
