"""Tests every command's exit code, for brew parity."""

from __future__ import annotations

import pytest

from brewery.cli.commands import cleanup as cleanup_mod
from brewery.cli.commands import install as install_mod
from brewery.cli.commands import link as link_mod
from brewery.cli.commands import pin as pin_mod
from brewery.cli.commands import uninstall as uninstall_mod
from brewery.cli.commands import upgrade as upgrade_mod
from brewery.core.errors import (
    EXIT_SYSTEM_ERROR,
    EXIT_TRANSIENT_ERROR,
    EXIT_USER_ERROR,
    BrewCommandError,
    PackageNotFoundError,
    SysError,
    TransientError,
    UserError,
)

EXIT_INTERRUPTED = 130

# (argv, module, attribute) for every command that reports per-item failures
_FAILING_COMMANDS = [
    pytest.param(
        ["install", "wget", "--yes"], install_mod, "install_packages", id="install"
    ),
    pytest.param(
        ["uninstall", "wget", "--yes"],
        uninstall_mod,
        "uninstall_packages",
        id="uninstall",
    ),
    pytest.param(
        ["upgrade", "wget", "--yes"], upgrade_mod, "upgrade_packages", id="upgrade"
    ),
    pytest.param(["cleanup"], cleanup_mod, "cleanup_packages", id="cleanup"),
]


def _failing_result(attr: str):
    """Build a stub whose result shape matches `attr` and reports one failure.

    Args:
        attr: The service function being stubbed.

    Returns:
        A stub with that function's return shape, carrying one failure.
    """
    failure = [("wget", "boom")]
    shapes = {
        "install_packages": ([], failure),
        "uninstall_packages": ([], failure),
        "upgrade_packages": ([], [], [], failure),
        "cleanup_packages": ([], failure),
    }
    result = shapes[attr]

    async def _stub(*args, **kwargs):
        return result

    return _stub


class TestFailureExitCodes:
    """Tests that a reported per-item failure sets brew's ofail exit code."""

    @pytest.mark.parametrize(("argv", "module", "attr"), _FAILING_COMMANDS)
    def test_a_reported_failure_exits_user_error(
        self, run, monkeypatch, argv, module, attr
    ) -> None:
        """Test that the command does its work, reports, then exits 1.

        This is brew's `ofail` semantics: a partial failure is still a failure,
        but the command does not abort the items that would have succeeded.
        """
        monkeypatch.setattr(module, attr, _failing_result(attr))

        result = run(argv)

        assert result.exit_code == EXIT_USER_ERROR
        assert "wget" in result.output

    @pytest.mark.parametrize(("argv", "module", "attr"), _FAILING_COMMANDS)
    def test_a_clean_run_exits_zero(self, run, monkeypatch, argv, module, attr) -> None:
        """Test that the same commands exit 0 when nothing failed."""
        shapes = {
            "install_packages": ([], []),
            "uninstall_packages": ([], []),
            "upgrade_packages": ([], [], [], []),
            "cleanup_packages": ([], []),
        }

        async def _stub(*args, **kwargs):
            return shapes[attr]

        monkeypatch.setattr(module, attr, _stub)

        assert run(argv).exit_code == 0


class TestErrorTypeExitCodes:
    """Tests the error-class to exit-code mapping, through a real command."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            pytest.param(UserError("bad input"), EXIT_USER_ERROR, id="user"),
            pytest.param(
                PackageNotFoundError(package="nope"), EXIT_USER_ERROR, id="not_found"
            ),
            pytest.param(SysError("disk gone"), EXIT_SYSTEM_ERROR, id="sys"),
            pytest.param(
                BrewCommandError(command="brew install", returncode=1, error="x"),
                EXIT_SYSTEM_ERROR,
                id="brew_command",
            ),
            pytest.param(
                TransientError("network"), EXIT_TRANSIENT_ERROR, id="transient"
            ),
            pytest.param(RuntimeError("unexpected"), EXIT_SYSTEM_ERROR, id="unknown"),
        ],
    )
    def test_the_error_class_decides_the_code(
        self, run, monkeypatch, error, expected
    ) -> None:
        """Test that each error family maps to its documented exit code."""

        async def _boom(*args, **kwargs):
            raise error

        monkeypatch.setattr(install_mod, "install_packages", _boom)

        assert run(["install", "wget", "--yes"]).exit_code == expected

    def test_an_interrupt_exits_with_the_shell_convention(
        self, run, monkeypatch
    ) -> None:
        """Test that Ctrl-C exits 130 and says how to resume."""

        async def _interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(install_mod, "install_packages", _interrupt)

        result = run(["install", "wget", "--yes"])

        assert result.exit_code == EXIT_INTERRUPTED
        assert "brewery install <name>" in result.output


class TestSynchronousCommandExitCodes:
    """Tests the commands whose services are not coroutines."""

    def test_pin_exits_user_error_on_a_failure(self, run, monkeypatch) -> None:
        """Test that a failed pin sets the exit code."""
        monkeypatch.setattr(
            pin_mod.pin_service,
            "pin_packages",
            lambda repo, names: ([], [], [("wget", "not installed")]),
        )

        assert run(["pin", "wget"]).exit_code == EXIT_USER_ERROR

    def test_link_exits_user_error_on_a_failure(self, run, monkeypatch) -> None:
        """Test that a failed link sets the exit code."""
        monkeypatch.setattr(
            link_mod.link_service,
            "link_packages",
            lambda repo, names, **kw: ([], [], [("wget", "conflict")]),
        )

        assert run(["link", "wget"]).exit_code == EXIT_USER_ERROR


class TestSuccessExitCodes:
    """Tests that the read-only commands never invent a non-zero code."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["list"], id="list"),
            pytest.param(["outdated"], id="outdated"),
            pytest.param(["search", "wget"], id="search"),
            pytest.param(["config", "show"], id="config_show"),
            pytest.param(["config", "path"], id="config_path"),
        ],
    )
    def test_a_read_only_command_exits_zero(self, run, argv) -> None:
        """Test that reading state is never itself a failure."""
        assert run(argv).exit_code == 0
