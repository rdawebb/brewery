"""Unit tests for the shared CLI command error boundary.

Drives a throwaway ExtendedTyper app through CliRunner rather than the real
commands, so each branch of `command_error` is exercised in isolation.
"""

from __future__ import annotations

import re
from typing import Annotated

import pytest
from typer.testing import CliRunner
from typer_extensions import ExtendedTyper

from brewery.cli.error_formatting import (
    EXIT_INTERRUPTED,
    CommandFailed,
    command_error,
)
from brewery.core.errors import (
    EXIT_SYSTEM_ERROR,
    EXIT_USER_ERROR,
    AlreadyInstalledWarning,
    PackageNotFoundError,
)
from brewery.core.models import PackageKind

pytestmark = pytest.mark.unit

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI styling so substring tests survive colorised Rich output.

    Args:
        text: The text to strip.

    Returns:
        The text with ANSI styling stripped.
    """
    return _ANSI.sub("", text)


@pytest.fixture
def app() -> ExtendedTyper:
    """A fresh app carrying one command per boundary branch."""
    app = ExtendedTyper()

    @app.command()
    @command_error()
    def bare() -> None:
        """Bare command."""
        raise PackageNotFoundError(package="nope")

    @app.command()
    @command_error(interrupt_hint="brewery hinted <name>")
    def hinted() -> None:
        """Hinted command."""
        raise KeyboardInterrupt

    @app.command()
    @command_error()
    def unhinted() -> None:
        """Unhinted command."""
        raise KeyboardInterrupt

    @app.command()
    @command_error(warnings=(AlreadyInstalledWarning,))
    def warned() -> None:
        """Warned command."""
        raise AlreadyInstalledWarning(package="wget")

    @app.command()
    @command_error()
    def unwarned() -> None:
        """Unwarned command."""
        raise AlreadyInstalledWarning(package="wget")

    @app.command()
    @command_error()
    def exploding() -> None:
        """Exploding command."""
        raise RuntimeError("boom")

    @app.command()
    @command_error()
    def clean() -> None:
        """Clean command."""
        print("did the thing")

    @app.command()
    @command_error()
    def failing() -> None:
        """Command that raises CommandFailed after reporting, as pin/link do."""
        raise CommandFailed

    @app.command()
    @command_error()
    def succeeding() -> None:
        """Command whose failure list is empty, so it never raises."""
        failures: list[tuple[str, str]] = []
        if failures:
            raise CommandFailed
        print("did the thing")

    return app


class TestErrorMapping:
    """A raised BrewError still reaches handle_error through the wrapper."""

    def test_package_not_found_exits_user_error(self, app) -> None:
        result = runner.invoke(app, ["bare"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "Package Not Found" in result.output

    def test_package_not_found_prints_search_suggestion(self, app) -> None:
        result = runner.invoke(app, ["bare"])

        assert "brewery search" in result.output

    def test_unexpected_exception_exits_system_error(self, app) -> None:
        result = runner.invoke(app, ["exploding"])

        assert result.exit_code == EXIT_SYSTEM_ERROR
        assert "Unexpected error" in result.output

    def test_clean_command_is_untouched(self, app) -> None:
        result = runner.invoke(app, ["clean"])

        assert result.exit_code == 0
        assert "did the thing" in result.output


class TestKeyboardInterrupt:
    """Ctrl-C exits 130, and names the resume command when one is configured."""

    def test_hinted_exits_130_and_names_the_command(self, app) -> None:
        result = runner.invoke(app, ["hinted"])

        assert result.exit_code == EXIT_INTERRUPTED
        assert "Interrupted" in result.output
        assert "brewery hinted" in result.output

    def test_unhinted_exits_130_silently(self, app) -> None:
        result = runner.invoke(app, ["unhinted"])

        assert result.exit_code == EXIT_INTERRUPTED
        assert "Re-run" not in result.output


class TestWarnings:
    """Declared warning types print and exit 0; undeclared ones stay failures."""

    def test_declared_warning_exits_zero(self, app) -> None:
        result = runner.invoke(app, ["warned"])

        assert result.exit_code == 0
        assert "⚠" in result.output
        assert "wget" in result.output

    def test_undeclared_warning_falls_through_to_handle_error(self, app) -> None:
        result = runner.invoke(app, ["unwarned"])

        # AlreadyInstalledWarning subclasses UserError.
        assert result.exit_code == EXIT_USER_ERROR


# `Annotated` metadata is evaluated as a string under `from __future__ import
# annotations`, so it must resolve against module globals — a test-local app
# would not be visible. One app per command: a single-command Typer app is
# invoked directly, which is the surface these tests assert on.
_sig_app = ExtendedTyper()
_enum_app = ExtendedTyper()


class TestSignatureTransparency:
    """Typer must see the wrapped function's signature, not `*args, **kwargs`.

    Typer resolves parameters via `inspect.signature(fn, eval_str=True)` and
    `get_type_hints(fn)`; both follow `__wrapped__`. If that ever stops holding,
    options vanish from --help while the default invocation still works, so
    assert on the introspected surface directly.
    """

    def test_options_and_arguments_survive_the_wrapper(self) -> None:
        """Test that options and arguments survive the wrapper."""

        @_sig_app.command()
        @command_error()
        def decorated(
            names: Annotated[list[str], _sig_app.Argument()],
            kind: Annotated[PackageKind | None, _sig_app.Option("--kind")] = None,
            yes: Annotated[bool, _sig_app.Option("--yes", "-y")] = False,
        ) -> None:
            """Decorated docstring."""
            print(f"names={names} kind={kind} yes={yes}")

        help_out = _plain(runner.invoke(_sig_app, ["--help"]).output)
        assert "--kind" in help_out
        assert "--yes" in help_out
        assert "NAMES" in help_out
        assert "Decorated docstring." in help_out

    def test_enum_option_still_parses(self) -> None:
        """Test that enum options still parse correctly."""

        @_enum_app.command()
        @command_error()
        def decorated(
            kind: Annotated[PackageKind | None, _enum_app.Option("--kind")] = None,
        ) -> None:
            print(f"kind={kind}")

        result = runner.invoke(_enum_app, ["--kind", "cask"])

        assert result.exit_code == 0
        assert "PackageKind.CASK" in result.output

    def test_name_is_preserved_for_known_commands_derivation(self) -> None:
        """Test that name is preserved for known commands derivation."""

        # main._derive_known_commands falls back to `info.callback.__name__` for
        # commands registered without an explicit `name=`
        @command_error()
        def some_command() -> None: ...

        assert some_command.__name__ == "some_command"


class TestCommandFailed:
    """`CommandFailed` maps to the user-error exit code without reprinting."""

    def test_command_failed_exits_user_error(self, app) -> None:
        """A hard failure exits 1, matching brew's `ofail` semantics."""
        result = runner.invoke(app, ["failing"])

        assert result.exit_code == EXIT_USER_ERROR

    def test_command_failed_prints_nothing_itself(self, app) -> None:
        """The sentinel is silent; the command owns its own failure report."""
        result = runner.invoke(app, ["failing"])

        assert "CommandFailed" not in result.output
        assert "Unexpected error" not in result.output

    def test_no_failures_exits_clean(self, app) -> None:
        """A command that never raises the sentinel runs to completion."""
        result = runner.invoke(app, ["succeeding"])

        assert result.exit_code == 0
        assert "did the thing" in result.output

    def test_typer_exit_would_be_swallowed_by_the_boundary(self) -> None:
        """Guards why partial failures use CommandFailed, not ExtendedTyper.Exit.

        `click.exceptions.Exit` subclasses RuntimeError, so `command_error`'s
        catch-all treats it as an unexpected error and remaps it to exit 2. A
        dedicated sentinel caught before the catch-all avoids that.
        """
        app = ExtendedTyper()

        @app.command()
        @command_error()
        def raises_typer_exit() -> None:
            raise app.Exit(EXIT_USER_ERROR)

        assert isinstance(app.Exit(EXIT_USER_ERROR), Exception)
        assert runner.invoke(app, []).exit_code == EXIT_SYSTEM_ERROR
