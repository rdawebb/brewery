"""Unit tests for the brew passthrough: argv forwarding and exit-code parity."""

from __future__ import annotations

import pytest

from brewery.cli import main as main_mod
from brewery.cli.main import _brew_passthrough
from brewery.core.errors import EXIT_SYSTEM_ERROR, BrewCommandError
from brewery.core.shell import BrewOutput, BrewResult


@pytest.fixture
def brew_calls(monkeypatch) -> list[dict]:
    """Patch `run_brew` in main with a recorder that reports success.

    Args:
        monkeypatch: The monkeypatch fixture.

    Returns:
        A list accumulating one dict of call kwargs per invocation.
    """
    calls: list[dict] = []

    async def _run_brew(args, *, output, check, timeout) -> BrewResult:
        calls.append(
            {"args": args, "output": output, "check": check, "timeout": timeout}
        )

        return BrewResult(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(main_mod, "run_brew", _run_brew)

    return calls


def _returning(monkeypatch, returncode: int) -> None:
    """Patch `run_brew` to report `returncode` and nothing else.

    Args:
        monkeypatch: The monkeypatch fixture.
        returncode: The exit code brew should be pretended to have returned.
    """

    async def _run_brew(args, **kwargs) -> BrewResult:
        return BrewResult(stdout="", stderr="", returncode=returncode)

    monkeypatch.setattr(main_mod, "run_brew", _run_brew)


def _raising(monkeypatch, exc: BaseException) -> None:
    """Patch `run_brew` to raise `exc`.

    Args:
        monkeypatch: The monkeypatch fixture.
        exc: The exception the stubbed `run_brew` should raise.
    """

    async def _run_brew(args, **kwargs) -> BrewResult:
        raise exc

    monkeypatch.setattr(main_mod, "run_brew", _run_brew)


class TestReturnCode:
    """The passthrough's only real contract: brew's exit code, verbatim."""

    @pytest.mark.parametrize("code", [0, 1, 2, 17, 127, 130, 255])
    def test_brew_return_code_is_forwarded(self, monkeypatch, code) -> None:
        """Test that whatever brew exits with is what the passthrough returns."""
        _returning(monkeypatch, code)

        assert _brew_passthrough(["doctor"]) == code

    def test_brew_missing_reports_a_system_error(self, monkeypatch, capsys) -> None:
        """Test that a BrewCommandError becomes EXIT_SYSTEM_ERROR, not a traceback.

        `run_brew` raises BrewCommandError(returncode=127) when brew is absent
        from PATH; the passthrough sits outside any `@command_error` boundary,
        so it has to translate that itself.
        """
        _raising(
            monkeypatch, BrewCommandError(message="brew not found", returncode=127)
        )

        assert _brew_passthrough(["doctor"]) == EXIT_SYSTEM_ERROR
        assert "brew not found on PATH" in capsys.readouterr().out

    def test_interrupt_reports_the_shell_convention(self, monkeypatch) -> None:
        """Test that Ctrl-C during a passthrough exits 130, as a shell would."""
        _raising(monkeypatch, KeyboardInterrupt())

        assert _brew_passthrough(["doctor"]) == 130


class TestInvocation:
    """How the passthrough calls brew, which decides what the user sees."""

    def test_argv_is_forwarded_verbatim(self, brew_calls) -> None:
        """Test that the whole argv reaches brew unrewritten and unreordered."""
        _brew_passthrough(["services", "restart", "--all"])

        assert brew_calls[0]["args"] == ["services", "restart", "--all"]

    def test_output_is_inherited_and_unchecked_and_untimed(self, brew_calls) -> None:
        """Test the three kwargs that make a passthrough behave like running brew.

        INHERIT streams brew's own output (so interactive commands work),
        check=False lets a non-zero code return rather than raise, and no
        timeout means a long `brew upgrade` is not killed underneath the user.
        """
        _brew_passthrough(["doctor"])

        assert brew_calls[0]["output"] is BrewOutput.INHERIT
        assert brew_calls[0]["check"] is False
        assert brew_calls[0]["timeout"] is None

    def test_logging_is_configured_before_brew_runs(self, monkeypatch) -> None:
        """Test that the passthrough configures logging itself.

        `main()` exits down this path before `app()`, so the Typer setup
        callback never runs and nothing else would configure logging.
        """
        configured: list[object] = []
        monkeypatch.setattr(
            main_mod,
            "configure_logging",
            lambda console_level=None: configured.append(console_level),
        )
        _returning(monkeypatch, 0)

        _brew_passthrough(["doctor"])

        assert configured, "logging was never configured on the passthrough path"

    def test_the_invocation_is_logged_with_its_result(self, monkeypatch) -> None:
        """Test that the passthrough leaves a log record naming argv and the code."""
        events: list[dict] = []
        monkeypatch.setattr(
            main_mod.log, "info", lambda **kw: events.append(kw), raising=False
        )
        _returning(monkeypatch, 3)

        _brew_passthrough(["services", "list"])

        assert events == [
            {
                "event": "brew_passthrough",
                "argv": "services list",
                "returncode": 3,
            }
        ]
