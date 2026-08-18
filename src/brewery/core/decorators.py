"""Decorators for common functionality (logging, timing, error handling)."""

from __future__ import annotations

import functools
import inspect
import time
from collections.abc import Awaitable, Callable, Sized
from typing import Any, TypeVar, cast

from brewery.core.errors import TransientError
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.retry import retry_async

log: BreweryLogger = get_logger(name=__name__)

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Any])


def _log_context(
    sig: inspect.Signature | None,
    log_args: list[str],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Resolve the logged arguments of one call to a name -> value mapping.

    Args:
        sig: The decorated function's signature, or None when nothing is logged.
        log_args: Parameter names to pick out of the call.
        args: The call's positional arguments.
        kwargs: The call's keyword arguments.

    Returns:
        The subset of the call's arguments named by `log_args`; empty when there is
        nothing to log, or when the call does not match the signature.
    """
    if sig is None:
        return {}

    try:
        bound = sig.bind(*args, **kwargs)

    except TypeError:
        return {}

    bound.apply_defaults()

    return {k: bound.arguments[k] for k in log_args if k in bound.arguments}


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

        # Resolved once per decoration; skipped entirely when nothing is logged
        sig: inspect.Signature | None = inspect.signature(func) if log_args else None

        @functools.wraps(wrapped=func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            start: float = time.perf_counter()

            log_context = _log_context(sig, log_args, args, kwargs)
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
                log.exception(
                    event=f"{event_prefix}_failed",
                    error=str(e),
                    duration_ms=duration_ms,
                    **log_context,
                )
                raise

        @functools.wraps(wrapped=func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start: float = time.perf_counter()

            log_context = _log_context(sig, log_args, args, kwargs)
            log.info(event=f"{event_prefix}_start", **log_context)

            try:
                result = func(*args, **kwargs)  # No await

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
                log.exception(
                    event=f"{event_prefix}_failed",
                    error=str(e),
                    duration_ms=duration_ms,
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

    def decorator(func: F) -> F:
        """Decorator to apply retry logic to the function."""
        if not inspect.iscoroutinefunction(func):
            raise TypeError("retry_on_transient only supports async functions")

        @functools.wraps(wrapped=func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            """Retry the wrapped async function on transient errors.

            Args:
                *args: Positional arguments to pass to the wrapped function.
                **kwargs: Keyword arguments to pass to the wrapped function.

            Returns:
                The result of the wrapped function after retries.
            """
            return await retry_async(
                functools.partial(func, *args, **kwargs),
                retry_on=lambda exc: isinstance(exc, TransientError),
                attempts=max_retries,
                base=base_delay,
                factor=backoff,
                label=getattr(func, "__name__", repr(func)),
            )

        return cast(typ=F, val=wrapper)

    return decorator
