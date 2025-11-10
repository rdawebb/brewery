"""Module defining custom exceptions for the Brewery application."""

from __future__ import annotations

import asyncio
import functools
import time
from typing import Any, Callable, Self, TypeVar

from brewery.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# Exit Codes
EXIT_SUCCESS = 0
EXIT_USER_ERROR = 1
EXIT_SYSTEM_ERROR = 2
EXIT_TRANSIENT_ERROR = 3


class BrewError(Exception):
    """Base exception class with context propagation.
    
    All exceptions in the Brewery should inherit from this class.
    Context is a dictionary that accumulates relevant information
    as the exception propagates up the call stack.

    Example:
        raise BrewError("An error occurred", context={"package": "foo"})

        # Or with context propagation
        try:
            ...
        except BrewError as e:
            raise e.with_context(operation="install", user="admin")
    """
    def __init__(self, message: str, context: dict[str, Any] | None = None) -> None:
        self.message = message
        self.context = context or {}
        super().__init__(message)

    def with_context(self, **new_context: Any) -> Self:
        """Returns a new exception instance with updated context.

        Args:
            **new_context: Additional context to add to the exception.

        Returns:
            A new instance of the exception with merged context.
        """
        self.context.update(new_context)
        return self
    
    def __str__(self) -> str:
        """String representation of the exception including context."""
        if self.context:
            context_str = ", ".join(f"{k}={v}" for k, v in self.context.items())
            return f"{self.message} [{context_str}]"
        return self.message
    

class TransientError(BrewError):
    """Errors that should be retried immediately.
    
    These errors are typically due to temporary conditions such as
    network issues or resource unavailability.
    
    Operations raising this exception should be idempotent.
    """
    pass


class UserError(BrewError):
    """Errors caused by user actions or inputs.

    These errors indicate that the user has made a mistake or provided 
    invalid input, and should not be retried without correction.

    CLI should display helpful messages to guide the user.
    """
    pass


class SystemError(BrewError):
    """Errors due to system-level issues.

    These errors indicate problems with the system environment, such as
    file system errors, permission issues, or other unexpected conditions.

    These errors may require user intervention or system fixes, and
    CLI should display diagnostic information for troubleshooting.
    """
    pass


## Specific Exceptions ##

class BrewCommandError(UserError):
    """Brew command returned a non-zero exit code."""
    pass


class BrewTimeoutError(TransientError):
    """Brew command timed out."""
    pass


class PackageNotFoundError(UserError):
    """Requested package was not found in the repository."""
    pass


class CacheError(SystemError):
    """Errors related to cache access or corruption."""
    pass


def retry_on_transient(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry async functions on transient errors with exponential backoff.

    Args:
        max_retries: Maximum number of retries before giving up.
        base_delay: Initial delay between retries in seconds.
        backoff: Multiplier for delay to implement exponential backoff.

    Returns:
        A decorator that applies the retry logic to the decorated function.

    Example:
        @retry_on_transient(max_retries=5, base_delay=2.0)
        async def fetch_data():
            ...

    Note:
        - Only retries on TransientError exceptions.
        - Logs each retry attempt with context information.
        - Works with sync and async functions.
        - Delays: 1s, 2s, 4s with default settings.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        """Decorator to apply retry logic to the function."""
        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: TransientError | None = None

            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except TransientError as e:
                    last_error = e

                    if attempt == max_retries:
                        log.error(
                            "retry_exhausted",
                            function=func.__name__,
                            attempts=max_retries,
                            error=str(e),
                            context=e.context
                        )
                        raise

                    delay = base_delay * (backoff ** (attempt - 1))
                    log.warning(
                        "retry_attempt",
                        function=func.__name__,
                        attempt=attempt,
                        max_attempts=max_retries,
                        delay_seconds=delay,
                        error=str(e),
                        context=e.context
                    )
                    await asyncio.sleep(delay)

            raise last_error  # type: ignore
        
        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            last_error: TransientError | None = None

            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except TransientError as e:
                    last_error = e

                    if attempt == max_retries:
                        log.error(
                            "retry_exhausted",
                            function=func.__name__,
                            attempts=max_retries,
                            error=str(e),
                            context=e.context
                        )
                        raise

                    delay = base_delay * (backoff ** (attempt - 1))
                    log.warning(
                        "retry_attempt",
                        function=func.__name__,
                        attempt=attempt,
                        max_attempts=max_retries,
                        delay_seconds=delay,
                        error=str(e),
                        context=e.context
                    )
                    time.sleep(delay)

            raise last_error  # type: ignore
        
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        else:
            return sync_wrapper  # type: ignore
        
    return decorator


# CLI Error Message Templates

ERROR_TEMPLATES = {
    PackageNotFoundError: {
        "❌ Package Not Found: {package}\n"
            "   Suggestion: Try 'brewery search {package}' to find similar packages"
    },
    BrewTimeoutError: {
        "⚠️ Command timed out after {timeout}s: {command}\n"
            "   The operation took too long - this may be due to network issues"
    },
    BrewCommandError: {
        "⚠️ Brew command failed: {command}\n"
            "   Exit Code: {returncode}\n"
            "   Error: {error}"
    },
    CacheError: {
        "⚠️ Cache error: {error}\n"
            "   Location: {path}\n"
            "   Fix: Check file permissions or clear cache with 'brewery cache clear'"
    },
    TransientError: {
        "⚠️ Temporary failure: {message}\n"
            "   This may resolve itself - try again in a moment"
    },
    UserError: {
        "❌ {message}"
    },
    SystemError: {
        "⚠️ System error: {message}\n"
            "   Please check your system configuration and try again"
    },
}

def format_error_message(error: BrewError) -> str:
    """Formats an error message for CLI display based on the error type.

    Args:
        error: The BrewError instance to format.

    Returns:
        A formatted string message for CLI display.
    """
    template = ERROR_TEMPLATES.get(type(error), ERROR_TEMPLATES[BrewError])
    try:
        return template.format(message=error.message, **error.context)
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