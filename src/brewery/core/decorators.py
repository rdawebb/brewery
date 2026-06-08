"""Decorators for common functionality (logging, timing, error handling)."""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Sized
from typing import Any, Awaitable, Callable, TypeVar, cast

from brewery.core.errors import TransientError
from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])


def log_operation(
    event_prefix: str,
    log_args: list[str] | None = None,
    log_result: bool = False,
):
    """Decorator to log operation start/completion with timing.

    Args:
        event_prefix: Prefix for log event names.
        log_args: List of argument names to include in logs.
        log_result: If True, log the result (only simple types and lengths).
    """
    if log_args is None:
        log_args: list[str] = []

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        is_async = inspect.iscoroutinefunction(func)

        @functools.wraps(wrapped=func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            start: float = time.perf_counter()

            sig = inspect.signature(func)
            try:
                bound_args = sig.bind(*args, **kwargs)
                bound_args.apply_defaults()
                log_context = {
                    k: bound_args.arguments[k]
                    for k in (log_args or [])
                    if k in bound_args.arguments
                }

            except TypeError:
                log_context = {}

            log.info(event=f"{event_prefix}_start", **log_context)

            try:
                result = await func(*args, **kwargs)

                duration_ms = int((time.perf_counter() - start) * 1000)
                log_event_data: dict = {
                    "event": f"{event_prefix}_complete",
                    "duration_ms": duration_ms,
                    **log_context,
                }

                # Optionally log result
                if log_result and result is not None:
                    if isinstance(result, (str, int)):
                        log_event_data["result"] = result
                    elif isinstance(result, Sized):
                        log_event_data["count"] = len(result)

                log.info(**log_event_data)

                return result

            except Exception as e:
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.error(
                    event=f"{event_prefix}_failed",
                    error=str(object=e),
                    duration_ms=duration_ms,
                    exc_info=True,
                    **log_context,
                )
                raise

        @functools.wraps(wrapped=func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start: float = time.perf_counter()
            sig = inspect.signature(func)
            try:
                bound_args = sig.bind(*args, **kwargs)
                bound_args.apply_defaults()
                log_context = {
                    k: bound_args.arguments[k]
                    for k in (log_args or [])
                    if k in bound_args.arguments
                }
            except TypeError:
                log_context = {}

            log.info(event=f"{event_prefix}_start", **log_context)

            try:
                result = func(*args, **kwargs)  # no await

                duration_ms = int((time.perf_counter() - start) * 1000)
                log_event_data: dict = {
                    "event": f"{event_prefix}_complete",
                    "duration_ms": duration_ms,
                    **log_context,
                }
                if log_result and result is not None:
                    if isinstance(result, (str, int)):
                        log_event_data["result"] = result
                    elif isinstance(result, Sized):
                        log_event_data["count"] = len(result)

                log.info(**log_event_data)
                return result

            except Exception as e:
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.error(
                    event=f"{event_prefix}_failed",
                    error=str(object=e),
                    duration_ms=duration_ms,
                    exc_info=True,
                    **log_context,
                )
                raise

        return async_wrapper if is_async else sync_wrapper

    return decorator


def retry_on_transient(
    max_retries: int = 3, base_delay: float = 1.0, backoff: float = 2.0
) -> Callable[[F], F]:
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
        - Works only with async functions.
        - Delays: 1s, 2s, 4s with default settings.
    """
    import asyncio

    def decorator(func: F) -> F:
        """Decorator to apply retry logic to the function."""
        if not inspect.iscoroutinefunction(func):
            raise TypeError("retry_on_transient only supports async functions")

        @functools.wraps(wrapped=func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except TransientError as e:
                    if attempt == max_retries:
                        log.error(
                            event="retry_exhausted",
                            function=getattr(func, "__name__", repr(func)),
                            attempts=max_retries,
                            error=str(object=e),
                            context=getattr(e, "context", {}),
                        )
                        raise

                    delay: float = base_delay * (backoff ** (attempt - 1))
                    log.warning(
                        event="retry_attempt",
                        function=getattr(func, "__name__", repr(func)),
                        attempt=attempt,
                        max_attempts=max_retries,
                        delay_seconds=delay,
                        error=str(object=e),
                        context=getattr(e, "context", {}),
                    )
                    await asyncio.sleep(delay)

        return cast(typ=F, val=wrapper)

    return decorator
