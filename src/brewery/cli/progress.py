"""Rich-backed progress reporter for the native install/upgrade pipeline."""

from __future__ import annotations

import asyncio

from rich.console import Console
from rich.filesize import decimal
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn

# Outcome values (Outcome.<...>.value) that mean the package did not install
_FAILED_OUTCOMES = frozenset({"failed", "skipped_dep_failed"})

_REVEAL_DELAY = 0.5  # seconds before the live display appears


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
            SpinnerColumn(finished_text=" "),
            TextColumn("{task.description}"),
            BarColumn(bar_width=None),
            console=console,
            transient=True,
            refresh_per_second=10,
        )
        self._overall: TaskID | None = None
        self._tasks: dict[str, TaskID] = {}
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
            self._overall = self._progress.add_task("Installing", total=total)

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
            tid = self._progress.add_task(name, total=None)
            self._tasks[name] = tid

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
            self._progress.update(
                tid,
                description=f"{name}  [dim]downloading {got}{size}[/dim]",
                total=total,
                completed=done or 0,
            )

        elif stage == "install":
            self._progress.update(
                tid, description=f"{name}  [dim]installing[/dim]", total=None
            )

    def finish(self, name: str, outcome: str) -> None:
        """Mark `name` done: freeze its row with a glyph, advance the overall bar.

        Args:
            name: The formula name.
            outcome: The terminal `Outcome` value.
        """
        failed = outcome in _FAILED_OUTCOMES
        glyph = "[red]✗[/red]" if failed else "[green]✓[/green]"
        tid = self._tasks.get(name)
        if tid is not None:
            # completed == total freezes the spinner to its finished_text
            self._progress.update(
                tid, description=f"{glyph} {name}", total=1, completed=1
            )
            self._progress.stop_task(tid)

        if self._overall is not None:
            self._progress.advance(self._overall, 1)

    def end(self) -> None:
        """Cancel a pending reveal and stop the live display (clears it)."""
        if self._reveal_handle is not None:
            self._reveal_handle.cancel()
            self._reveal_handle = None

        if self._started:
            self._progress.stop()
            self._started = False
