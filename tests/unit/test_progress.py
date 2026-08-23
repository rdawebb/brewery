"""Unit tests for the install/upgrade progress reporter."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from brewery.cli.progress import (
    _GLYPH_DONE,
    _GLYPH_DOWNLOAD,
    _GLYPH_DOWNLOADED,
    _GLYPH_FAILED,
    _GLYPH_INSTALL,
    _MAX_VISIBLE,
    ProgressReporter,
    _ActivityColumn,
    make_reporter,
)
from brewery.providers.orchestrator import Outcome


@pytest.fixture
def tty() -> Console:
    """A console that reports itself as a terminal but writes to a buffer.

    Returns:
        A Console rendering into StringIO with force_terminal set.
    """
    return Console(file=StringIO(), force_terminal=True, width=100)


@pytest.fixture
def reporter(tty) -> ProgressReporter:
    """A reporter over the buffered tty console, with no live display started.

    Args:
        tty: The buffered terminal console fixture.

    Returns:
        A ProgressReporter ready to accept begin/update/finish.
    """
    return ProgressReporter(tty)


def _fields(reporter: ProgressReporter, name: str) -> dict:
    """Read back the Rich task fields the reporter set for one package row.

    Args:
        reporter: The reporter holding the task.
        name: The formula name whose row to read.

    Returns:
        The task's `fields` dict.
    """
    tid = reporter._tasks[name]

    return next(t for t in reporter._progress.tasks if t.id == tid).fields


class TestMakeReporter:
    """Tests make_reporter's tty gate, the whole non-interactive suppression contract."""

    def test_no_reporter_when_output_is_not_a_terminal(self) -> None:
        """Test that a piped or redirected console gets no live display at all.

        Spinner and redraw control codes corrupt captured output, so the
        pipeline has to run silently rather than render into a pipe.
        """
        piped = Console(file=StringIO(), force_terminal=False)

        assert make_reporter(piped) is None

    def test_reporter_when_output_is_a_terminal(self, tty) -> None:
        """Test that an interactive console does get a reporter."""
        assert isinstance(make_reporter(tty), ProgressReporter)


class TestBegin:
    """Tests the overall anchor bar and the deferred reveal."""

    async def test_multi_package_run_gets_an_overall_bar(self, reporter) -> None:
        """Test that a transaction of more than one package gets the anchor row."""
        reporter.begin(3)

        assert reporter._overall is not None
        assert reporter._total == 3
        assert len(reporter._progress.tasks) == 1

        reporter.end()

    async def test_single_package_run_has_no_overall_bar(self, reporter) -> None:
        """Test that a lone formula is not given an anchor bar counting to one."""
        reporter.begin(1)

        assert reporter._overall is None
        assert reporter._progress.tasks == []

        reporter.end()

    async def test_the_display_is_not_revealed_immediately(self, reporter) -> None:
        """Test that begin arms the deferred reveal rather than starting the display.

        A fast install should finish before the display ever appears, so the
        terminal is not flickered for a transaction lasting 100ms.
        """
        reporter.begin(2)

        assert reporter._started is False
        assert reporter._reveal_handle is not None

        reporter.end()

    async def test_end_cancels_a_pending_reveal(self, reporter) -> None:
        """Test that a run finishing before the reveal delay leaves no timer armed."""
        reporter.begin(2)
        reporter.end()

        assert reporter._reveal_handle is None
        assert reporter._started is False

    async def test_the_reveal_starts_the_display_once(self, reporter) -> None:
        """Test that a slow run does get its live display, and only one of them."""
        reporter.begin(2)

        reporter._reveal()

        assert reporter._started is True
        assert reporter._reveal_handle is None

        reporter._reveal()  # A second firing must not restart the display

        assert reporter._started is True

        reporter.end()

    async def test_end_stops_a_revealed_display(self, reporter) -> None:
        """Test that the transient live region is torn down when the run ends."""
        reporter.begin(2)
        reporter._reveal()

        reporter.end()

        assert reporter._started is False


class TestUpdate:
    """Tests per-row glyph and activity transitions."""

    def test_download_with_a_known_length_uses_a_bar(self, reporter) -> None:
        """Test that a Content-Length turns the row's indicator into a real bar."""
        reporter.update("wget", "download", done=50, total=100)

        assert _fields(reporter, "wget")["activity"] == "bar"
        assert _fields(reporter, "wget")["glyph"] == _GLYPH_DOWNLOAD
        assert _fields(reporter, "wget")["status"] == "50 bytes/100 bytes"

    def test_download_with_no_length_falls_back_to_a_spinner(self, reporter) -> None:
        """Test that a server with no Content-Length gets an indeterminate spinner."""
        reporter.update("wget", "download", done=50, total=None)

        assert _fields(reporter, "wget")["activity"] == "spinner"
        assert _fields(reporter, "wget")["status"] == "50 bytes"

    def test_a_completed_download_flips_the_glyph(self, reporter) -> None:
        """Test that reaching the total marks the row as downloaded, not downloading."""
        reporter.update("wget", "download", done=100, total=100)

        assert _fields(reporter, "wget")["glyph"] == _GLYPH_DOWNLOADED

    def test_an_unknown_byte_count_renders_a_placeholder(self, reporter) -> None:
        """Test that a download reporting no byte count still renders a status."""
        reporter.update("wget", "download", done=None, total=None)

        assert _fields(reporter, "wget")["status"] == "?"

    def test_the_install_stage_takes_over_the_row(self, reporter) -> None:
        """Test that a package moving to install swaps glyph, status, and activity."""
        reporter.update("wget", "download", done=100, total=100)
        reporter.update("wget", "install")

        fields = _fields(reporter, "wget")
        assert fields["glyph"] == _GLYPH_INSTALL
        assert fields["status"] == "installing"
        assert fields["activity"] == "spinner"

    def test_a_row_is_created_once_and_reused(self, reporter) -> None:
        """Test that repeated updates for one package do not stack up rows."""
        for done in (10, 20, 30):
            reporter.update("wget", "download", done=done, total=100)

        assert len(reporter._progress.tasks) == 1


class TestFinish:
    """Tests terminal outcomes, and which of them the reporter treats as failures."""

    @pytest.mark.parametrize("outcome", list(Outcome), ids=lambda o: o.name)
    def test_every_outcome_renders_the_right_glyph(self, reporter, outcome) -> None:
        """Test that only SKIPPED_DEP_FAILED and FAILED render a cross.

        Pins the membership of _FAILED_OUTCOMES independently of the set itself,
        so widening it by mistake shows up as a tick turning into a cross.
        """
        expected_failure = outcome in {Outcome.FAILED, Outcome.SKIPPED_DEP_FAILED}
        reporter.update("wget", "install")

        reporter.finish("wget", outcome.value)

        glyph = _fields(reporter, "wget")["glyph"]
        assert glyph == (_GLYPH_FAILED if expected_failure else _GLYPH_DONE)

    def test_a_finished_row_blanks_its_indicator(self, reporter) -> None:
        """Test that a completed row keeps only its glyph, with no trailing bar."""
        reporter.update("wget", "install")

        reporter.finish("wget", Outcome.NATIVE.value)

        assert _fields(reporter, "wget")["activity"] == ""

    async def test_the_overall_bar_advances_and_counts(self, reporter) -> None:
        """Test that each finish moves the anchor bar and its n/total caption."""
        reporter.begin(2)
        reporter.update("wget", "install")
        reporter.finish("wget", Outcome.NATIVE.value)

        overall = next(t for t in reporter._progress.tasks if t.id == reporter._overall)
        assert overall.completed == 1
        assert overall.fields["status"] == "1/2"

        reporter.end()

    async def test_finishing_an_unseen_package_still_advances_the_overall(
        self, reporter
    ) -> None:
        """Test that a package that failed before its first update is still counted."""
        reporter.begin(2)

        reporter.finish("never-started", Outcome.FAILED.value)

        overall = next(t for t in reporter._progress.tasks if t.id == reporter._overall)
        assert overall.completed == 1

        reporter.end()


class TestTrim:
    """Tests the eviction arithmetic that keeps the live region from scrolling."""

    def _run(self, reporter: ProgressReporter, count: int) -> None:
        """Take `count` packages all the way through to a finished row.

        Args:
            reporter: The reporter to drive.
            count: How many packages to start and finish.
        """
        for i in range(count):
            reporter.update(f"pkg{i}", "install")
            reporter.finish(f"pkg{i}", Outcome.NATIVE.value)

    def test_package_rows_are_capped_without_an_anchor(self, reporter) -> None:
        """Test that finished rows are evicted once the cap is passed."""
        self._run(reporter, _MAX_VISIBLE + 5)

        assert len(reporter._progress.tasks) == _MAX_VISIBLE

    async def test_the_anchor_row_is_excluded_from_the_cap(self, reporter) -> None:
        """Test that the overall bar does not cost a package its slot.

        `_trim` subtracts the anchor before comparing, so a run with an overall
        bar shows _MAX_VISIBLE packages plus the anchor, not one fewer package.
        """
        reporter.begin(_MAX_VISIBLE + 5)
        self._run(reporter, _MAX_VISIBLE + 5)

        assert len(reporter._progress.tasks) == _MAX_VISIBLE + 1

        reporter.end()

    def test_the_oldest_finished_row_is_the_one_dropped(self, reporter) -> None:
        """Test that eviction is oldest-first, so the newest rows stay visible."""
        self._run(reporter, _MAX_VISIBLE + 1)

        names = {t.fields["name"] for t in reporter._progress.tasks}
        assert "pkg0" not in names
        assert f"pkg{_MAX_VISIBLE}" in names

    def test_unfinished_rows_are_never_evicted(self, reporter) -> None:
        """Test that a run wider than the cap keeps in-flight rows on screen.

        `_trim` only pops from the finished deque, so exceeding the cap with
        live downloads overflows rather than hiding work still in progress.
        """
        for i in range(_MAX_VISIBLE + 4):
            reporter.update(f"pkg{i}", "download", done=1, total=10)

        assert len(reporter._progress.tasks) == _MAX_VISIBLE + 4


class TestActivityColumn:
    """Tests the one column that renders bar, spinner, or nothing."""

    @pytest.mark.parametrize(
        ("activity", "expect_blank"),
        [
            pytest.param("bar", False, id="bar"),
            pytest.param("spinner", False, id="spinner"),
            pytest.param("", True, id="finished"),
            pytest.param(None, True, id="unset"),
        ],
    )
    def test_render_is_keyed_off_the_activity_field(
        self, reporter, tty, activity, expect_blank
    ) -> None:
        """Test that only a live activity renders an indicator cell."""
        reporter.update("wget", "download", done=1, total=10)
        reporter._progress.update(reporter._tasks["wget"], activity=activity)
        task = next(t for t in reporter._progress.tasks if t.fields["name"] == "wget")

        rendered = _ActivityColumn().render(task)

        assert (tty.render_str(str(rendered)).plain == "") is expect_blank
