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


def _result_fields(result: Any, log_result: bool) -> dict[str, Any]:
    """Describe a return value for the completion log line.

    Args:
        result: The decorated call's return value.
        log_result: Whether the caller asked for the result to be described.

    Returns:
        `{"result": ...}` for simple scalars, `{"count": ...}` for anything sized,
        and an empty mapping otherwise.
    """
    if not log_result or result is None:
        return {}

    if isinstance(result, (str, int)):
        return {"result": result}

    if isinstance(result, Sized):
        return {"count": len(result)}

    return {}


def _elapsed_ms(start: float) -> int:
    """Milliseconds since a `time.perf_counter()` reading.

    Args:
        start: The reading taken when the operation began.

    Returns:
        The elapsed time in whole milliseconds.
    """
    return int((time.perf_counter() - start) * 1000)


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

        def _report_start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict:
            context = _log_context(sig, log_args, args, kwargs)
            log.info(event=f"{event_prefix}_start", **context)

            return context

        def _report_complete(result: Any, start: float, context: dict) -> None:
            log.info(
                event=f"{event_prefix}_complete",
                duration_ms=_elapsed_ms(start),
                **context,
                **_result_fields(result, log_result),
            )

        def _report_failed(exc: Exception, start: float, context: dict) -> None:
            log.exception(
                event=f"{event_prefix}_failed",
                error=str(exc),
                duration_ms=_elapsed_ms(start),
                **context,
            )

        @functools.wraps(wrapped=func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            start: float = time.perf_counter()
            context = _report_start(args, kwargs)

            try:
                result = await func(*args, **kwargs)

            except Exception as e:
                _report_failed(e, start, context)
                raise

            _report_complete(result, start, context)

            return result

        @functools.wraps(wrapped=func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start: float = time.perf_counter()
            context = _report_start(args, kwargs)

            try:
                result = func(*args, **kwargs)  # No await

            except Exception as e:
                _report_failed(e, start, context)
                raise

            _report_complete(result, start, context)

            return result

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
