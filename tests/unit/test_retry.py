"""Unit tests for the shared async retry helper."""

from __future__ import annotations

import asyncio

import pytest

from brewery.core.retry import RETRYABLE_STATUS, backoff_delay, retry_async


@pytest.fixture
def delays(monkeypatch) -> list[float]:
    """Capture the backoff sleeps instead of performing them.

    Returns:
        The list of delays recorded during retries.
    """
    recorded: list[float] = []

    async def _record(d: float) -> None:
        """No-op sleep function that records the delay."""
        recorded.append(d)

    monkeypatch.setattr("brewery.core.retry.asyncio.sleep", _record)

    return recorded


def _always(_exc: Exception) -> bool:
    """Always returns True, indicating the exception should be retried."""
    return True


async def test_returns_first_success(delays) -> None:
    """A call that succeeds immediately is not retried and does not sleep."""
    calls = {"n": 0}

    async def op() -> str:
        """No-op function that increments a call counter and returns "ok"."""
        calls["n"] += 1
        return "ok"

    assert await retry_async(op, retry_on=_always) == "ok"
    assert calls["n"] == 1
    assert delays == []


async def test_retries_then_succeeds(delays) -> None:
    """Attempts continue until one succeeds."""
    calls = {"n": 0}

    async def op() -> str:
        """Increments a call counter and raises an error until it reaches 3, then returns "ok"."""
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("later")

        return "ok"

    assert await retry_async(op, retry_on=_always) == "ok"
    assert calls["n"] == 3


async def test_exhausts_and_reraises_the_last_error(delays) -> None:
    """Once attempts run out the final exception propagates unwrapped."""
    calls = {"n": 0}

    async def op() -> None:
        """Increments a call counter and raises a RuntimeError."""
        calls["n"] += 1
        raise RuntimeError(f"boom {calls['n']}")

    with pytest.raises(RuntimeError, match="boom 3"):
        await retry_async(op, retry_on=_always, attempts=3)

    assert calls["n"] == 3
    assert delays == [1.0, 2.0]  # None after the final attempt


async def test_rejected_exception_propagates_immediately(delays) -> None:
    """An exception the predicate rejects is not retried."""
    calls = {"n": 0}

    async def op() -> None:
        """Raises a ValueError."""
        calls["n"] += 1
        raise ValueError("genuine")

    with pytest.raises(ValueError):
        await retry_async(op, retry_on=lambda exc: isinstance(exc, KeyError))

    assert calls["n"] == 1
    assert delays == []


async def test_cancellation_is_never_retried(delays) -> None:
    """CancelledError passes straight through, even with a permissive predicate."""
    calls = {"n": 0}

    async def op() -> None:
        """Raises a CancelledError."""
        calls["n"] += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_async(op, retry_on=_always)

    assert calls["n"] == 1


async def test_zero_attempts_is_rejected() -> None:
    """`attempts` below 1 is a programming error, not a silent no-op."""
    calls = {"n": 0}

    async def op() -> None:
        """No-op function that increments a call counter."""
        calls["n"] += 1

    with pytest.raises(ValueError, match="attempts must be >= 1"):
        await retry_async(op, retry_on=_always, attempts=0)

    assert calls["n"] == 0


def test_backoff_delay_respects_cap() -> None:
    """The cap bounds the exponential growth."""
    kw = {"base": 1.0, "factor": 2.0, "jitter": 0.0}
    assert [backoff_delay(n, cap=8.0, **kw) for n in range(1, 7)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        8.0,
        8.0,
    ]


def test_backoff_delay_jitter_stays_in_band() -> None:
    """Jitter only ever adds, and never more than its width."""
    for _ in range(50):
        d = backoff_delay(1, base=0.5, factor=2.0, cap=None, jitter=0.25)
        assert 0.5 <= d < 0.75


def test_retryable_status_set() -> None:
    """The shared status set is the one both HTTP call sites expect."""
    assert RETRYABLE_STATUS == frozenset({429, 500, 502, 503, 504})
