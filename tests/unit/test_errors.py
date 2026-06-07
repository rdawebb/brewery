"""Unit tests for Brewery error handling and custom classes."""

from __future__ import annotations

import pytest

from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewCommandError,
    BrewError,
    BrewTimeoutError,
    CacheError,
    PackageNotFoundError,
    PinnedPackageWarning,
    SysError,
    TransientError,
    UserError,
    format_error_message,
    suggest_search,
)

pytestmark = pytest.mark.unit


class TestBrewError:
    """Test BrewError and its subclasses."""

    def test_message_and_empty_context(self) -> None:
        """Test that a simple message with no context is handled correctly."""
        err = BrewError("boom")
        assert err.message == "boom"
        assert err.context == {}
        assert str(err) == "boom"

    def test_str_includes_context(self) -> None:
        """Test that context is included in the string representation."""
        err = BrewError("boom", context={"package": "foo"})
        assert str(err) == "boom [package=foo]"

    def test_with_context_merges_and_returns_self(self) -> None:
        """Test that with_context merges context and returns self."""
        err = BrewError("boom", context={"a": 1})
        returned = err.with_context(b=2)
        assert returned is err
        assert err.context == {"a": 1, "b": 2}

    def test_with_context_overwrites_existing_key(self) -> None:
        """Test that with_context overwrites existing keys."""
        err = BrewError("boom", context={"a": 1})
        err.with_context(a=2)
        assert err.context["a"] == 2


class TestExceptionHierarchy:
    """Test the exception hierarchy."""

    def test_command_and_timeout_are_transient(self) -> None:
        """Test that BrewCommandError and BrewTimeoutError are transient."""
        assert isinstance(BrewCommandError(), TransientError)
        assert isinstance(BrewTimeoutError(), TransientError)

    def test_package_warnings_are_user_errors(self) -> None:
        """Test that PackageNotFoundError, AlreadyInstalledWarning, and PinnedPackageWarning are UserErrors."""
        assert isinstance(PackageNotFoundError(), UserError)
        assert isinstance(AlreadyInstalledWarning(), UserError)
        assert isinstance(PinnedPackageWarning(), UserError)

    def test_cache_error_is_sys_error(self) -> None:
        """Test that CacheError is a SysError."""
        assert isinstance(CacheError(), SysError)


class TestDefaultMessages:
    """Test the default messages for BrewCommandError and BrewTimeoutError."""

    def test_brew_command_error_default_message(self) -> None:
        """Test the default message for BrewCommandError."""
        err = BrewCommandError(command="brew install foo", returncode=1, error="nope")
        assert "exit code 1" in err.message
        assert err.context["command"] == "brew install foo"
        assert err.context["returncode"] == 1
        assert err.context["error"] == "nope"

    def test_brew_command_error_unknown_returncode(self) -> None:
        """Test the default message for BrewCommandError with an unknown return code."""
        assert "unknown" in BrewCommandError().message

    def test_timeout_default_message(self) -> None:
        """Test the default message for BrewTimeoutError."""
        err = BrewTimeoutError(command="brew upgrade", timeout=30)
        assert "30s" in err.message
        assert err.context["timeout"] == 30

    def test_package_not_found_with_kind(self) -> None:
        """Test the message for PackageNotFoundError with a kind."""
        err = PackageNotFoundError(package="foo", kind="cask")
        assert "cask" in err.message
        assert "foo" in err.message
        assert err.context == {"package": "foo", "kind": "cask"}

    def test_already_installed_message(self) -> None:
        """Test the message for AlreadyInstalledWarning."""
        assert "already installed" in AlreadyInstalledWarning(package="foo").message

    def test_pinned_message(self) -> None:
        """Test the message for PinnedPackageWarning."""
        assert "pinned" in PinnedPackageWarning(package="foo").message

    def test_custom_message_overrides_default(self) -> None:
        """Test that a custom message overrides the default message."""
        assert BrewCommandError(message="custom").message == "custom"


class TestFormatErrorMessage:
    """Test the format_error_message function."""

    def test_package_not_found_template(self) -> None:
        """Test the template for PackageNotFoundError."""
        msg = format_error_message(PackageNotFoundError(package="foo"))
        assert "Package Not Found: foo" in msg
        assert "brewery search foo" in msg

    def test_brew_command_error_template(self) -> None:
        """Test the template for BrewCommandError."""
        msg = format_error_message(
            BrewCommandError(command="brew install foo", returncode=1, error="boom")
        )
        assert "brew install foo" in msg
        assert "1" in msg
        assert "boom" in msg

    def test_already_installed_template(self) -> None:
        """Test the template for AlreadyInstalledWarning."""
        msg = format_error_message(AlreadyInstalledWarning(package="foo"))
        assert "Already installed: foo" in msg

    def test_falls_back_to_base_template_for_user_error(self) -> None:
        """Test that a UserError falls back to the base template."""
        msg = format_error_message(UserError("bad input"))
        assert "bad input" in msg

    def test_unknown_subclass_uses_base_template(self) -> None:
        """Test that an unknown subclass uses the base template."""

        class WeirdError(BrewError):
            pass

        msg = format_error_message(WeirdError("strange"))
        assert "strange" in msg

    def test_missing_template_key_falls_back_gracefully(self) -> None:
        """Test that missing template keys fall back gracefully."""
        # Neither {error} nor {path} are in context, so format() raises KeyError
        # Should fall back to the bare message
        msg = format_error_message(CacheError(message="cache boom"))
        assert "cache boom" in msg


def test_suggest_search_mentions_package_and_site() -> None:
    """Test that suggest_search mentions the package and site."""
    out = suggest_search("foo")
    assert "brewery search foo" in out
    assert "formulae.brew.sh" in out
