"""Unit tests for CLI error-to-exit-code mapping."""

from __future__ import annotations

import pytest

from brewery.cli.main import handle_error
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
                EXIT_TRANSIENT_ERROR,
                id="brew_command_is_transient",
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
