"""CLI error message formatting for Brewery."""

from __future__ import annotations

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
)

ERROR_TEMPLATES: dict[type[BrewError], str] = {
    AlreadyInstalledWarning: (
        "⚠️ Already installed: {package}\n"
        "   Suggestion: Try 'brewery update {package}' to update the package"
    ),
    PinnedPackageWarning: (
        "⚠️ Package is pinned: {package}\n"
        "   Suggestion: Try 'brewery unpin {package}' to unpin the package before upgrading"
    ),
    PackageNotFoundError: (
        "❌ Package Not Found: {package}\n"
        "   Suggestion: Try 'brewery search {package}' to find similar packages"
    ),
    BrewTimeoutError: (
        "⚠️ Command timed out after {timeout}s: {command}\n"
        "   The operation took too long - this may be due to network issues"
    ),
    BrewCommandError: (
        "⚠️ Brew command failed: {command}\n   Exit Code: {returncode}\n   Error: {error}"
    ),
    CacheError: (
        "⚠️ Cache error: {error}\n"
        "   Location: {path}\n"
        "   Fix: Check file permissions or clear cache with 'brewery cache clear'"
    ),
    TransientError: (
        "⚠️ Temporary failure: {message}\n   This may resolve itself - try again in a moment"
    ),
    UserError: "❌ {message}",
    SysError: (
        "⚠️ System error: {message}\n   Please check your system configuration and try again"
    ),
    BrewError: "❌ {message}",
}


def format_error_message(error: BrewError) -> str:
    """Formats an error message for CLI display based on the error type.

    Args:
        error: The BrewError instance to format.

    Returns:
        A formatted string message for CLI display.
    """
    for cls in type(error).__mro__:
        if issubclass(cls, BrewError) and cls in ERROR_TEMPLATES:
            template = ERROR_TEMPLATES[cls]
            break
    else:
        template = ERROR_TEMPLATES[BrewError]

    try:
        return template.format(message=error.message, **getattr(error, "context", {}))

    except KeyError:
        return f"❌ {error.message}"


def suggest_search(package_name: str) -> str:
    """Suggest a search command for a missing package.

    Args:
        package_name: The name of the missing package.

    Returns:
        Formatted search suggestion string.
    """
    return (
        f"\n💡 Suggestions:\n"
        f"   • Try 'brewery search {package_name}'\n"
        "   • Check for spelling and try again\n"
        "   • Visit https://formulae.brew.sh/ to browse available packages\n"
    )
