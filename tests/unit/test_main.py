"""Unit tests for CLI error-to-exit-code mapping."""

from __future__ import annotations

import re

import pytest

from brewery.cli.error_formatting import ERROR_TEMPLATES, handle_error, suggest_search
from brewery.cli.main import KNOWN_COMMANDS
from brewery.core.errors import (
    EXIT_SYSTEM_ERROR,
    EXIT_TRANSIENT_ERROR,
    EXIT_USER_ERROR,
    BrewCommandError,
    BrewError,
    CacheError,
    PackageNotFoundError,
    PinnedPackageWarning,
    SysError,
    TransientError,
    UserError,
)

pytestmark = pytest.mark.unit


class TestHandleError:
    """Tests for handle_error exit-code mapping."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            pytest.param(TransientError("boom"), EXIT_TRANSIENT_ERROR, id="transient"),
            pytest.param(
                BrewCommandError(returncode=1),
                EXIT_SYSTEM_ERROR,
                id="brew_command_is_system",
            ),
            pytest.param(UserError("bad input"), EXIT_USER_ERROR, id="user"),
            pytest.param(
                PackageNotFoundError(package="nope"),
                EXIT_USER_ERROR,
                id="package_not_found_is_user",
            ),
            pytest.param(
                PinnedPackageWarning("pinned"),
                EXIT_USER_ERROR,
                id="pinned_warning_is_user",
            ),
            pytest.param(SysError("disk"), EXIT_SYSTEM_ERROR, id="system"),
            pytest.param(CacheError("cache"), EXIT_SYSTEM_ERROR, id="cache_is_system"),
            # Base BrewError is none of Transient/User/Sys -> exercises the else branch.
            pytest.param(
                BrewError("generic"),
                EXIT_USER_ERROR,
                id="unknown_brewerror_defaults_to_user",
            ),
            # Arbitrary non-BrewError exceptions map to the system code.
            pytest.param(
                ValueError("unexpected"),
                EXIT_SYSTEM_ERROR,
                id="non_brewerror_is_system",
            ),
        ],
    )
    def test_handle_error(self, error, expected) -> None:
        """Test the handle_error function."""
        assert handle_error(error) == expected


# Commands a suggestion may name that brewery does not register itself; these
# resolve through the brew passthrough in main(). Drop entries as they go native.
PASSTHROUGH_SUGGESTIONS: frozenset[str] = frozenset()

_SUGGESTED_COMMAND = re.compile(r"brewery ([a-z][a-z-]*)")


def _suggested_commands(text: str) -> set[str]:
    """Extract every 'brewery <command>' named in a suggestion string."""
    return set(_SUGGESTED_COMMAND.findall(text))


class TestSuggestedCommandsExist:
    """Suggestions must name a command the user can actually run."""

    @pytest.mark.parametrize(
        "template",
        [pytest.param(t, id=cls.__name__) for cls, t in ERROR_TEMPLATES.items()],
    )
    def test_error_template_commands_are_runnable(self, template: str) -> None:
        """Every command named in an error template is registered or passes through."""
        for command in _suggested_commands(template):
            assert command in KNOWN_COMMANDS | PASSTHROUGH_SUGGESTIONS, (
                f"error template suggests 'brewery {command}', which is neither a "
                f"registered brewery command nor a known brew passthrough"
            )

    def test_suggest_search_commands_are_runnable(self) -> None:
        """The missing-package suggestion names only runnable commands."""
        for command in _suggested_commands(suggest_search(package_name="wget")):
            assert command in KNOWN_COMMANDS | PASSTHROUGH_SUGGESTIONS
