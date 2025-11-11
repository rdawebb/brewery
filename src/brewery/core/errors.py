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

class BrewCommandError(TransientError):
    """Brew command returned a non-zero exit code.

    Typically indicates:
        - Network issues
        - Brew service outages
        - Rate limiting
        - Corrupted local Brew installation

    This will be retried automatically by the caller.
    """
    def __init__(
        self,
        message: str | None = None,
        command: str | None = None,
        returncode: int | None = None,
        error: str | None = None,
        context: dict[str, Any] | None = None
    ) -> None:
        """Initialise BrewCommandError with detailed context.
        
        Args:
            message: Optional custom error message.
            command: The brew command that was executed.
            returncode: The exit code returned by the command.
            error: The error output from the command.
            context: Additional context information.
        """
        ctx = context or {}
        if command:
            ctx["command"] = command
        if returncode is not None:
            ctx["returncode"] = returncode
        if error:
            ctx["error"] = error if error is not None else ""
        
        if message is None:
            message = f"Brew command failed with exit code {returncode or 'unknown'}"

        super().__init__(message, context=ctx)


class BrewTimeoutError(TransientError):
    """Brew command timed out.

    Typically indicates:
        - Slow network conditions
        - Brew server overload
        - Very large package downloads
        - System resource constraints

    This will be retried automatically by the caller.
    """
    def __init__(
        self,
        message: str | None = None,
        command: str | None = None,
        timeout: int | None = None,
        context: dict[str, Any] | None = None
    ) -> None:
        """Initialise BrewTimeoutError with detailed context.
        
        Args:
            message: Optional custom error message.
            command: The brew command that was executed.
            timeout: The timeout threshold in seconds.
            context: Additional context information.
        """
        ctx = context or {}
        if command:
            ctx["command"] = command
        if timeout is not None:
            ctx["timeout"] = timeout
        
        if message is None:
            message = f"Brew command timed out after {timeout or 'unknown'}s"

        super().__init__(message, context=ctx)


class PackageNotFoundError(UserError):
    """Requested package was not found in the repository.
    
    This is UserError - do not retry without changing the package name.
    """
    def __init__(
        self,
        message: str | None = None,
        package: str | None = None,
        kind: str | None = None,
        context: dict[str, Any] | None = None
    ) -> None:
        """Initialise PackageNotFoundError with detailed context.
        
        Args:
            message: Optional custom error message.
            package: The name of the package that was not found.
            kind: The kind of package (e.g., formula, cask).
            context: Additional context information.
        """
        ctx = context or {}
        if package:
            ctx["package"] = package
        if kind:
            ctx["kind"] = kind
        
        if message is None:
            kind_str = f" {kind}" if kind else ""
            message = f"Package{kind_str} '{package or 'unknown'}' not found"

        super().__init__(message, context=ctx)


class CacheError(SystemError):
    """Errors related to cache access or corruption.
    
    Typically indicates:
        - File system permission issues
        - Disk space exhaustion
        - Corrupted cache files
        - Read-only file system
        
    This is a SystemError - may require user/system intervention.
    """
    def __init__(
        self,
        message: str | None = None,
        key: str | None = None,
        namespace: str | None = None,
        path: str | None = None,
        operation: str | None = None,
        context: dict[str, Any] | None = None
    ) -> None:
        """Initialise CacheError with detailed context.
        
        Args:
            message: Optional custom error message.
            key: The cache key involved in the error.
            namespace: The cache namespace or directory.
            path: The file path involved in the error.
            operation: The cache operation being performed.
            context: Additional context information.
        """
        ctx = context or {}
        if key:
            ctx["key"] = key
        if namespace:
            ctx["namespace"] = namespace
        if path:
            ctx["path"] = path
        if operation:
            ctx["operation"] = operation

        if message is None:
            op_str = f"{operation}" if operation else ""
            message = f"Cache {op_str} operation failed"

        super().__init__(message, context=ctx)


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
                                context=getattr(e, "context", {})
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
                            context=getattr(e, "context", {})
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
                                context=getattr(e, "context", {})
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
                            context=getattr(e, "context", {})
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
    PackageNotFoundError: (
        "❌ Package Not Found: {package}\n"
        "   Suggestion: Try 'brewery search {package}' to find similar packages"
    ),
    BrewTimeoutError: (
        "⚠️ Command timed out after {timeout}s: {command}\n"
        "   The operation took too long - this may be due to network issues"
    ),
    BrewCommandError: (
        "⚠️ Brew command failed: {command}\n"
        "   Exit Code: {returncode}\n"
        "   Error: {error}"
    ),
    CacheError: (
        "⚠️ Cache error: {error}\n"
        "   Location: {path}\n"
        "   Fix: Check file permissions or clear cache with 'brewery cache clear'"
    ),
    TransientError: (
        "⚠️ Temporary failure: {message}\n"
        "   This may resolve itself - try again in a moment"
    ),
    UserError: (
        "❌ {message}"
    ),
    SystemError: (
        "⚠️ System error: {message}\n"
        "   Please check your system configuration and try again"
    ),
    BrewError: (
        "❌ {message}"
    ),
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