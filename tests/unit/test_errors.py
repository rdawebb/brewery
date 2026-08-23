"""Unit tests for Brewery error handling and custom classes."""

from __future__ import annotations

import re

import pytest

from brewery.cli.error_formatting import (
    ERROR_TEMPLATES,
    format_error_message,
    suggest_search,
)
from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewCommandError,
    BrewError,
    BrewTimeoutError,
    CacheError,
    LinkError,
    PackageNotFoundError,
    PinnedPackageWarning,
    SysError,
    TransientError,
    UserError,
)


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

    @pytest.mark.parametrize(
        ("exc", "base"),
        [
            pytest.param(BrewCommandError(), SysError, id="command_is_sys"),
            pytest.param(BrewTimeoutError(), TransientError, id="timeout_is_transient"),
            pytest.param(PackageNotFoundError(), UserError, id="not_found_is_user"),
            pytest.param(
                AlreadyInstalledWarning(), UserError, id="already_installed_is_user"
            ),
            pytest.param(PinnedPackageWarning(), UserError, id="pinned_is_user"),
            pytest.param(CacheError(), SysError, id="cache_is_sys"),
        ],
    )
    def test_exception_base_class(self, exc, base) -> None:
        """Test that the exception is an instance of the base class."""
        assert isinstance(exc, base)


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


# One representative, fully-populated instance per template, with a fragment of
# each template's own suggestion text
_TEMPLATE_CASES = [
    pytest.param(
        AlreadyInstalledWarning(package="foo"),
        "brewery upgrade foo",
        id="already_installed",
    ),
    pytest.param(PinnedPackageWarning(package="foo"), "brewery unpin foo", id="pinned"),
    pytest.param(
        PackageNotFoundError(package="foo"), "brewery search foo", id="not_found"
    ),
    pytest.param(
        BrewTimeoutError(command="brew install foo", timeout=30),
        "took too long",
        id="timeout",
    ),
    pytest.param(
        BrewCommandError(command="brew install foo", returncode=1, error="boom"),
        "Exit Code: 1",
        id="command_failed",
    ),
    pytest.param(
        CacheError(message="cache boom", path="/tmp/cache"),
        "delete /tmp/cache to rebuild the cache",
        id="cache",
    ),
    pytest.param(
        LinkError([("bin/foo", "other")]),
        "brewery link --overwrite",
        id="link",
    ),
    pytest.param(
        TransientError("network hiccup"), "try again in a moment", id="transient"
    ),
    pytest.param(UserError("bad input"), "bad input", id="user"),
    pytest.param(SysError("disk gone"), "check your system configuration", id="sys"),
    pytest.param(BrewError("something"), "something", id="base"),
]


class TestEveryTemplateRenders:
    """Tests every ERROR_TEMPLATES entry, rendered against a fully-populated error."""

    def test_the_table_covers_every_template(self) -> None:
        """Test that no template was added without a case in this class."""
        covered = {type(case.values[0]) for case in _TEMPLATE_CASES}

        assert covered == set(ERROR_TEMPLATES)

    @pytest.mark.parametrize(("error", "fragment"), _TEMPLATE_CASES)
    def test_the_template_survives_rendering(self, error, fragment) -> None:
        """Test that the template renders in full rather than degrading."""
        assert fragment in format_error_message(error)

    @pytest.mark.parametrize(("error", "fragment"), _TEMPLATE_CASES)
    def test_every_placeholder_is_satisfiable(self, error, fragment) -> None:
        """Test that a template only asks for keys its error type actually carries.

        `format_error_message` always supplies `message`; everything else has to
        come from the error's own context.
        """
        placeholders = set(re.findall(r"\{(\w+)\}", ERROR_TEMPLATES[type(error)]))

        assert placeholders <= {"message", *error.context}


class TestTemplateFallbackGuard:
    """Tests the KeyError guard, tested as a guard rather than as a contract."""

    def test_a_context_missing_a_placeholder_does_not_crash(self) -> None:
        """Test that an under-populated error still produces a usable message.

        This is a safety net, not an intended outcome, as the suggestion is lost.
        `TestEveryTemplateRenders` asserts the real behaviour.
        """
        msg = format_error_message(CacheError(message="cache boom"))

        assert msg == "❌ cache boom"


def test_suggest_search_mentions_package_and_site() -> None:
    """Test that suggest_search mentions the package and site."""
    out = suggest_search("foo")
    assert "brewery search foo" in out
    assert "formulae.brew.sh" in out
