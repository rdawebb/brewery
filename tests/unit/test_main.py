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

    def test_transient_error_returns_transient_code(self) -> None:
        """Test that a transient error maps to the transient exit code."""
        assert handle_error(TransientError("boom")) == EXIT_TRANSIENT_ERROR

    def test_brew_command_error_is_transient(self) -> None:
        """Test that a BrewCommandError (TransientError subclass) is transient."""
        assert handle_error(BrewCommandError(returncode=1)) == EXIT_TRANSIENT_ERROR

    def test_user_error_returns_user_code(self) -> None:
        """Test that a user error maps to the user exit code."""
        assert handle_error(UserError("bad input")) == EXIT_USER_ERROR

    def test_package_not_found_is_user_error(self) -> None:
        """Test that PackageNotFoundError maps to the user exit code."""
        err = PackageNotFoundError(package="nope")
        assert handle_error(err) == EXIT_USER_ERROR

    def test_pinned_warning_is_user_error(self) -> None:
        """Test that a PinnedPackageWarning maps to the user exit code."""
        assert handle_error(PinnedPackageWarning("pinned")) == EXIT_USER_ERROR

    def test_sys_error_returns_system_code(self) -> None:
        """Test that a system error maps to the system exit code."""
        assert handle_error(SysError("disk")) == EXIT_SYSTEM_ERROR

    def test_cache_error_is_system_error(self) -> None:
        """Test that a CacheError (SysError subclass) maps to the system code."""
        assert handle_error(CacheError("cache")) == EXIT_SYSTEM_ERROR

    def test_unknown_brewerror_subclass_defaults_to_user(self) -> None:
        """Test that a BrewError that is none of the three branches defaults to user.

        The base BrewError is not Transient/User/Sys, exercising the else branch.
        """
        assert handle_error(BrewError("generic")) == EXIT_USER_ERROR

    def test_non_brewerror_returns_system_code(self) -> None:
        """Test that an arbitrary non-BrewError exception maps to the system code."""
        assert handle_error(ValueError("unexpected")) == EXIT_SYSTEM_ERROR
