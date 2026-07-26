"""Retry policy for async calls."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

T = TypeVar("T")

# Transient HTTP statuses to retry
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def backoff_delay(
    attempt: int,
    *,
    base: float,
    factor: float,
    cap: float | None,
    jitter: float,
) -> float:
    """The delay to wait after a failed attempt.

    Args:
        attempt: The 1-based attempt that just failed.
        base: Delay after the first failure, in seconds.
        factor: Multiplier applied per subsequent attempt.
        cap: Upper bound on the pre-jitter delay, if any.
        jitter: Width of the uniform random addition, in seconds.

    Returns:
        The number of seconds to sleep.
    """
    delay = base * (factor ** (attempt - 1))
    if cap is not None:
        delay = min(delay, cap)

    return delay + random.random() * jitter if jitter else delay


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    retry_on: Callable[[Exception], bool],
    attempts: int = 3,
    base: float = 1.0,
    factor: float = 2.0,
    cap: float | None = None,
    jitter: float = 0.0,
    label: str = "",
) -> T:
    """Call `fn`, retrying while `retry_on` accepts the exception it raised.

    Args:
        fn: Zero-argument async callable to invoke.
        retry_on: Predicate deciding whether an exception is worth retrying.
            Anything it rejects propagates immediately.
        attempts: Total number of calls, including the first.
        base: Delay after the first failure, in seconds.
        factor: Multiplier applied per subsequent attempt.
        cap: Upper bound on the pre-jitter delay, if any.
        jitter: Width of the uniform random addition, in seconds.
        label: Name used in the retry logs.

    Returns:
        Whatever `fn` returns.

    Raises:
        ValueError: If `attempts` is less than 1.
        Exception: The last exception raised by `fn`, once attempts run out.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1, got {attempts}")

    # `except Exception` deliberately lets CancelledError through untouched
    for attempt in range(1, attempts + 1):
        try:
            return await fn()

        except Exception as exc:
            if not retry_on(exc):
                raise

            if attempt == attempts:
                log.error(
                    event="retry_exhausted",
                    operation=label or None,
                    attempts=attempts,
                    error=str(exc),
                    context=getattr(exc, "context", None),
                )

                raise

            delay = backoff_delay(
                attempt, base=base, factor=factor, cap=cap, jitter=jitter
            )
            log.warning(
                event="retry_attempt",
                operation=label or None,
                attempt=attempt,
                max_attempts=attempts,
                delay_seconds=delay,
                error=str(exc),
                context=getattr(exc, "context", None),
            )
            await asyncio.sleep(delay)

    raise AssertionError("unreachable")  # pragma: no cover
