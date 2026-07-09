"""Unit tests for the shared CLI command error boundary.

Drives a throwaway ExtendedTyper app through CliRunner rather than the real
commands, so each branch of `command_error` is exercised in isolation.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner
from typer_extensions import ExtendedTyper

from brewery.cli.error_formatting import EXIT_INTERRUPTED, command_error
from brewery.core.errors import (
    EXIT_SYSTEM_ERROR,
    EXIT_USER_ERROR,
    AlreadyInstalledWarning,
    PackageNotFoundError,
)
from brewery.core.models import PackageKind

pytestmark = pytest.mark.unit

runner = CliRunner()


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


class TestSignatureTransparency:
    """Typer must see the wrapped function's signature, not `*args, **kwargs`.

    Typer resolves parameters via `inspect.signature(fn, eval_str=True)` and
    `get_type_hints(fn)`; both follow `__wrapped__`. If that ever stops holding,
    options vanish from --help while the default invocation still works, so
    assert on the introspected surface directly.
    """

    def test_options_and_arguments_survive_the_wrapper(self) -> None:
        app = ExtendedTyper()

        @app.command()
        @command_error()
        def decorated(
            names: list[str] = app.Argument(...),
            kind: PackageKind | None = app.Option(None, "--kind"),
            yes: bool = app.Option(False, "--yes", "-y"),
        ) -> None:
            """Decorated docstring."""
            print(f"names={names} kind={kind} yes={yes}")

        help_out = runner.invoke(app, ["--help"]).output
        assert "--kind" in help_out
        assert "--yes" in help_out
        assert "NAMES" in help_out
        assert "Decorated docstring." in help_out

    def test_enum_option_still_parses(self) -> None:
        app = ExtendedTyper()

        @app.command()
        @command_error()
        def decorated(kind: PackageKind | None = app.Option(None, "--kind")) -> None:
            print(f"kind={kind}")

        result = runner.invoke(app, ["--kind", "cask"])

        assert result.exit_code == 0
        assert "PackageKind.CASK" in result.output

    def test_name_is_preserved_for_known_commands_derivation(self) -> None:
        # main._derive_known_commands falls back to `info.callback.__name__` for
        # commands registered without an explicit `name=`
        @command_error()
        def some_command() -> None: ...

        assert some_command.__name__ == "some_command"
