"""Unit tests for the CLI's console output primitives."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from brewery.cli import output as output_mod
from brewery.cli.output import (
    _RESTORE_AND_ERASE,
    _SAVE_CURSOR,
    confirm_or_cancel,
    pkg_line,
    print_advisories,
    print_failures,
    print_result,
    spinner,
)
from brewery.core.models import Package, PackageKind


@pytest.fixture
def out(monkeypatch) -> StringIO:
    """Redirect the module-level console into a buffer and return it.

    `output.py` imports `console` from `cli.context` at module scope, so the
    substitution has to happen on the output module's own binding.

    Args:
        monkeypatch: The monkeypatch fixture.

    Returns:
        The buffer the console now writes into.
    """
    buf = StringIO()
    monkeypatch.setattr(
        output_mod, "console", Console(file=buf, force_terminal=False, width=100)
    )

    return buf


@pytest.fixture
def tty_out(monkeypatch) -> StringIO:
    """As `out`, but with a console that reports itself as a terminal.

    Args:
        monkeypatch: The monkeypatch fixture.

    Returns:
        The buffer the console now writes into.
    """
    buf = StringIO()
    monkeypatch.setattr(
        output_mod, "console", Console(file=buf, force_terminal=True, width=100)
    )

    return buf


def _package(name: str, versions: tuple[str, ...]) -> Package:
    """Build a minimal installed Package for the formatting helpers.

    Args:
        name: The package name.
        versions: The installed versions, newest first.

    Returns:
        A Package carrying just the fields the output helpers read.
    """
    return Package(name=name, kind=PackageKind.FORMULA, versions=list(versions))


class TestPkgLine:
    """Tests pkg_line, the one-line 'name version' formatter."""

    def test_the_active_version_is_appended(self) -> None:
        """Test that the first version is the one shown."""
        assert pkg_line(_package("wget", ("1.25.0", "1.24.5"))) == "wget 1.25.0"

    def test_a_package_with_no_versions_still_renders(self) -> None:
        """Test that an unversioned package degrades to a trailing space, not a crash."""
        assert pkg_line(_package("wget", ())) == "wget "


class TestSpinner:
    """Tests spinner, the shared status indicator for a long command stage."""

    def test_the_message_carries_the_requested_style(self, out) -> None:
        """Test that the caller's colour reaches the rendered status text."""
        status = str(spinner("Fetching...", style="cyan").status)

        assert "Fetching..." in status
        assert "cyan" in status


class TestPrintResult:
    """Tests print_result's header-always, bullets-optionally contract."""

    def test_the_header_prints_even_with_no_lines(self, out) -> None:
        """Test that a zero-count result is still reported."""
        print_result("✓ Installed 0 package(s)", [], style="green")

        assert "Installed 0 package(s)" in out.getvalue()

    def test_each_line_becomes_a_bullet(self, out) -> None:
        """Test that body lines are indented under the header, one bullet each."""
        print_result("✓ Done", ["wget 1.25.0", "curl 8.0"], style="green", bullet="-")

        text = out.getvalue()
        assert "  - wget 1.25.0" in text
        assert "  - curl 8.0" in text

    def test_line_style_replaces_the_dimmed_bullet_markup(self, out) -> None:
        """Test that a line_style styles the whole row rather than just the text.

        The two branches differ in markup, not just colour: without line_style
        the bullet is wrapped in [dim] tags, with it the whole line takes the
        style. Both must survive Rich's markup parsing intact.
        """
        print_result("h", ["wget"], style="green", bullet="•", line_style="red")

        assert "  • wget" in out.getvalue()


class TestPrintFailures:
    """Tests print_failures, including its empty-input no-op."""

    def test_nothing_is_printed_when_there_are_no_failures(self, out) -> None:
        """Test that a clean run prints no failure header at all."""
        print_failures("✗ Failed", [])

        assert out.getvalue() == ""

    def test_each_failure_names_itself_and_its_reason(self, out) -> None:
        """Test that the header and every (name, reason) pair are rendered."""
        print_failures("✗ Failed 2", [("wget", "404"), ("curl", "checksum")])

        text = out.getvalue()
        assert "Failed 2" in text
        assert "- wget - 404" in text
        assert "- curl - checksum" in text


class TestPrintAdvisories:
    """Tests print_advisories, the warn-but-do-not-fail channel."""

    def test_nothing_is_printed_when_there_are_no_advisories(self, out) -> None:
        """Test that an empty advisory list is silent."""
        print_advisories([])

        assert out.getvalue() == ""

    def test_each_advisory_is_flagged(self, out) -> None:
        """Test that advisories render with a warning glyph, not a failure one."""
        print_advisories([("wget", "already pinned")])

        assert "⚠ wget - already pinned" in out.getvalue()


class TestConfirmOrCancel:
    """Tests the four paths through the confirmation prompt."""

    def test_yes_short_circuits_without_prompting(self, out, monkeypatch) -> None:
        """Test that --yes proceeds without reading stdin at all."""

        def _never(*args, **kwargs):
            raise AssertionError("Confirm.ask was called despite yes=True")

        monkeypatch.setattr(output_mod.Confirm, "ask", _never)

        assert confirm_or_cancel("Proceed?", yes=True) is True

    def test_eof_declines_rather_than_raising(self, out, monkeypatch) -> None:
        """Test that a closed stdin cancels instead of crashing.

        This is what makes `brewery uninstall x` safe when stdin is not a
        terminal: no answer is read as "no", never as the default "yes".
        """
        monkeypatch.setattr(
            output_mod.Confirm, "ask", lambda *a, **kw: (_ for _ in ()).throw(EOFError)
        )

        assert confirm_or_cancel("Proceed?", yes=False) is False
        assert "Cancelled" in out.getvalue()

    def test_declining_prints_the_cancel_notice(self, out, monkeypatch) -> None:
        """Test that a plain "no" reports that nothing happened."""
        monkeypatch.setattr(output_mod.Confirm, "ask", lambda *a, **kw: False)

        assert confirm_or_cancel("Proceed?", yes=False, cancel_msg="• Nope") is False
        assert "Nope" in out.getvalue()

    def test_accepting_prints_nothing(self, out, monkeypatch) -> None:
        """Test that a "yes" adds no notice of its own."""
        monkeypatch.setattr(output_mod.Confirm, "ask", lambda *a, **kw: True)

        assert confirm_or_cancel("Proceed?", yes=False) is True
        assert "Cancelled" not in out.getvalue()

    @pytest.mark.parametrize("default", [True, False])
    def test_the_default_is_passed_through_to_the_prompt(
        self, out, monkeypatch, default
    ) -> None:
        """Test that the caller's bare-Enter answer reaches Confirm.ask.

        `uninstall` is the only caller passing default=False, and that keyword
        is what stops a bare Enter from deleting a package.
        """
        seen: list[bool] = []
        monkeypatch.setattr(
            output_mod.Confirm,
            "ask",
            lambda *a, default=True, **kw: seen.append(default) or default,
        )

        confirm_or_cancel("Proceed?", yes=False, default=default)

        assert seen == [default]

    def test_a_terminal_saves_and_erases_the_prompt(self, tty_out, monkeypatch) -> None:
        """Test that an interactive prompt is wiped once answered.

        The DECSC/DECRC pair leaves the scrollback clean; both halves have to
        be emitted or the cursor is left parked mid-screen.
        """
        monkeypatch.setattr(output_mod.Confirm, "ask", lambda *a, **kw: True)

        confirm_or_cancel("Proceed?", yes=False)

        text = tty_out.getvalue()
        assert _SAVE_CURSOR in text
        assert _RESTORE_AND_ERASE in text

    def test_a_pipe_emits_no_cursor_control_codes(self, out, monkeypatch) -> None:
        """Test that redirected output is never polluted with escape sequences."""
        monkeypatch.setattr(output_mod.Confirm, "ask", lambda *a, **kw: True)

        confirm_or_cancel("Proceed?", yes=False)

        text = out.getvalue()
        assert _SAVE_CURSOR not in text
        assert _RESTORE_AND_ERASE not in text
