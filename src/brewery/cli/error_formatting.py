"""CLI error message formatting for Brewery."""

from __future__ import annotations

import contextlib
import functools
import sys
from collections.abc import Callable
from typing import ParamSpec

from brewery.core.errors import (
    EXIT_SYSTEM_ERROR,
    EXIT_TRANSIENT_ERROR,
    EXIT_USER_ERROR,
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
from brewery.core.logging import BreweryLogger, get_logger

from .context import console

P = ParamSpec("P")

EXIT_INTERRUPTED = 130

log: BreweryLogger = get_logger(name=__name__)


class CommandFailed(Exception):
    """Signal that a command hard-failed on one or more items.

    Raised after the command has already printed its own failure report, so it
    carries no message. `command_error` maps it to `EXIT_USER_ERROR`, matching
    brew's `ofail` semantics: the command does its valid work, reports what
    failed, and exits non-zero.
    """


ERROR_TEMPLATES: dict[type[BrewError], str] = {
    AlreadyInstalledWarning: (
        "⚠️ Already installed: {package}\n"
        "   Suggestion: Try 'brewery upgrade {package}' to update the package"
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
        "   Fix: Check file permissions, or delete {path} to rebuild the cache"
    ),
    LinkError: (
        "❌ {message}\n"
        "   Suggestion: Try 'brewery link --overwrite <name>' to replace the existing files"
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


def handle_error(error: Exception) -> int:
    """Handle errors and return appropriate exit codes.

    Args:
        error: The exception to handle.

    Returns:
        An integer exit code.
    """
    if isinstance(error, BrewError):
        # A broken log sink must not stop the user seeing the error itself
        with contextlib.suppress(Exception):
            log.error(
                event="cli_error",
                error_type=type(error).__name__,
                message=error.message,
                context=getattr(error, "context", {}),
                exc_info=error,
            )

        console.print(f"\n{format_error_message(error)}\n", style="bold red")

        if isinstance(error, PackageNotFoundError):
            package: str = getattr(error, "context", {}).get("package", "")
            console.print(suggest_search(package_name=package), style="dim")

        if isinstance(error, TransientError):
            return EXIT_TRANSIENT_ERROR

        elif isinstance(error, UserError):
            return EXIT_USER_ERROR

        elif isinstance(error, SysError):
            return EXIT_SYSTEM_ERROR

        else:
            return EXIT_USER_ERROR

    else:
        log.error(event="unexpected_error", error=str(error), exc_info=error)
        console.print(f"\n⚠ Unexpected error occurred: {error}\n", style="bold red")
        return EXIT_SYSTEM_ERROR


def command_error(
    *,
    interrupt_hint: str | None = None,
    warnings: tuple[type[BrewError], ...] = (),
) -> Callable[[Callable[P, None]], Callable[P, None]]:
    """Wrap a CLI command body with standard error handling.

    Args:
        interrupt_hint: Command to suggest re-running after a Ctrl-C, e.g.
            "brewery install <name>". Omit for commands with nothing to resume.
        warnings: Error types that are advisory rather than failures. These print
            their message and exit 0. Checked before the catch-all.

    Returns:
        A decorator wrapping the command body.
    """

    def decorate(fn: Callable[P, None]) -> Callable[P, None]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> None:
            try:
                return fn(*args, **kwargs)

            # An empty `warnings` tuple never matches, so this is a no-op by default
            except warnings as e:
                console.print(f"\n⚠ {e.message}\n", style="bold yellow")

            except CommandFailed:
                sys.exit(EXIT_USER_ERROR)

            except KeyboardInterrupt:
                if interrupt_hint:
                    console.print(
                        f"\n⚠ Interrupted. Re-run [bold]{interrupt_hint}[/bold] "
                        "to complete it\n",
                        style="bold yellow",
                    )
                sys.exit(EXIT_INTERRUPTED)

            # Last-resort boundary: every command failure becomes an exit code
            except Exception as e:  # noqa: BLE001
                sys.exit(handle_error(error=e))

        return wrapper

    return decorate
