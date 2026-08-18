"""Unit tests for Brewery logging and retrydecorators."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from brewery.core import decorators
from brewery.core.decorators import log_operation, retry_on_transient
from brewery.core.errors import TransientError, UserError

pytestmark = pytest.mark.unit


@pytest.fixture
def no_sleep(monkeypatch) -> None:
    """Make asyncio.sleep a no-op so backoff delays don't slow the suite."""

    async def _instant(*_args, **_kwargs) -> None:
        """A no-op function that returns None.

        Args:
            *_args: Variable length argument list.
            **_kwargs: Arbitrary keyword arguments.
        """
        return

    monkeypatch.setattr(asyncio, "sleep", _instant)


class TestRetryOnTransient:
    """Test the retry_on_transient decorator."""

    async def test_returns_result_on_first_success(self) -> None:
        """Test that retry_on_transient returns the result on the first success."""
        calls = []

        @retry_on_transient(max_retries=3, base_delay=0)
        async def op() -> str:
            """Simulate an operation that may fail.

            Returns:
                The result of the operation.
            """
            calls.append(1)

            return "ok"

        assert await op() == "ok"
        assert len(calls) == 1

    async def test_retries_then_succeeds(self, no_sleep) -> None:
        """Test that retry_on_transient retries and succeeds after transient errors."""
        attempts = {"n": 0}

        @retry_on_transient(max_retries=3, base_delay=0)
        async def op() -> str:
            """Simulate an operation that may fail.

            Returns:
                str: The result of the operation.

            Raises:
                TransientError: If the operation fails transiently.
            """
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise TransientError("temporary")

            return "recovered"

        assert await op() == "recovered"
        assert attempts["n"] == 3

    async def test_exhausts_and_reraises(self, no_sleep) -> None:
        """Test that retry_on_transient exhausts retries and reraises on non-transient errors."""
        attempts = {"n": 0}

        @retry_on_transient(max_retries=3, base_delay=0)
        async def op() -> None:
            """Simulate an operation that may fail.

            Raises:
                TransientError: Always raises a TransientError.
            """
            attempts["n"] += 1
            raise TransientError("always fails")

        with pytest.raises(TransientError):
            await op()
        assert attempts["n"] == 3  # Should be exactly max_retries attempts

    async def test_does_not_retry_non_transient(self, no_sleep) -> None:
        """Test that retry_on_transient does not retry on non-transient errors."""
        attempts = {"n": 0}

        @retry_on_transient(max_retries=3, base_delay=0)
        async def op() -> None:
            """Simulate an operation that may fail.

            Raises:
                UserError: Always raises a UserError.
            """
            attempts["n"] += 1
            raise UserError("bad input")

        with pytest.raises(UserError):
            await op()
        assert attempts["n"] == 1  # Not retried

    async def test_backoff_delays_follow_schedule(self, monkeypatch) -> None:
        """Test that retry_on_transient backoff delays follow the specified schedule."""
        delays: list[float] = []

        async def _record(d) -> None:
            """Record the delay.

            Args:
                d: The delay to record.
            """
            delays.append(d)

        monkeypatch.setattr(asyncio, "sleep", _record)

        @retry_on_transient(max_retries=3, base_delay=1.0, backoff=2.0)
        async def op():
            raise TransientError("x")

        with pytest.raises(TransientError):
            await op()

        # Delays applied after attempts 1 and 2 (none after the final attempt)
        assert delays == [1.0, 2.0]

    def test_rejects_sync_function(self) -> None:
        """Test that retry_on_transient rejects sync functions."""
        with pytest.raises(TypeError):

            @retry_on_transient()
            def sync_op() -> int:
                """Simulate a synchronous operation that may fail.

                Returns:
                    Always returns 1.
                """
                return 1


class TestLogOperation:
    """Test the log_operation decorator."""

    async def test_returns_underlying_result(self) -> None:
        """Test that log_operation returns the underlying result."""

        @log_operation(event_prefix="thing")
        async def op(x) -> int:
            """Simulate an operation that may fail.

            Args:
                x: The input value.

            Returns:
                int: The result of the operation.
            """
            return x * 2

        assert await op(21) == 42

    async def test_reraises_exceptions(self) -> None:
        """Test that log_operation reraises exceptions."""

        @log_operation(event_prefix="thing")
        async def op() -> None:
            """Simulate an operation that may fail.

            Raises:
                ValueError: Always raises a ValueError.
            """
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await op()

    async def test_logs_start_and_complete(self, caplog) -> None:
        """Test that log_operation logs start and complete events."""

        import logging

        @log_operation(event_prefix="myop", log_args=["name"])
        async def op(name) -> str:
            """Simulate an operation that may fail.

            Args:
                name: The name to return.

            Returns:
                str: The name.
            """
            return "done"

        with caplog.at_level(logging.INFO, logger="brewery.core.decorators"):
            await op(name="foo")

        messages = [r.getMessage() for r in caplog.records]
        assert any("myop_start" in m for m in messages)
        assert any("myop_complete" in m for m in messages)

        # The named arg should appear in the start context
        assert any("name=foo" in m for m in messages)

    async def test_logs_failure_event(self, caplog) -> None:
        """Test that log_operation logs failure events."""

        import logging

        @log_operation(event_prefix="myop")
        async def op() -> None:
            """Simulate an operation that may fail.

            Raises:
                RuntimeError: Always raises a RuntimeError.
            """
            raise RuntimeError("kaboom")

        with (
            caplog.at_level(logging.ERROR, logger="brewery.core.decorators"),
            pytest.raises(RuntimeError),
        ):
            await op()

        assert any("myop_failed" in r.getMessage() for r in caplog.records)

    async def test_signature_is_resolved_once_per_decoration(self, monkeypatch) -> None:
        """Test that inspect.signature runs at decoration time, not per call."""
        import inspect

        calls: list[Any] = []
        real = inspect.signature

        def counting_signature(*args, **kwargs):
            """Count signature resolutions and delegate to the real one.

            Args:
                *args: Positional arguments for inspect.signature.
                **kwargs: Keyword arguments for inspect.signature.

            Returns:
                The resolved signature.
            """
            calls.append(args[0] if args else None)

            return real(*args, **kwargs)

        monkeypatch.setattr(decorators.inspect, "signature", counting_signature)

        @log_operation(event_prefix="myop", log_args=["name"])
        async def op(name) -> str:
            """Return the name it was given.

            Args:
                name: The name to return.

            Returns:
                The name.
            """
            return name

        assert len(calls) == 1

        await op(name="a")
        await op(name="b")
        assert len(calls) == 1

    async def test_no_log_args_skips_signature(self, monkeypatch, caplog) -> None:
        """Test that a decoration with no log_args never resolves a signature."""
        import logging

        def fail_signature(*args, **kwargs) -> None:
            """Stand in for inspect.signature.

            Args:
                *args: Positional arguments for inspect.signature.
                **kwargs: Keyword arguments for inspect.signature.

            Raises:
                AssertionError: Always; the signature should not be needed.
            """
            raise AssertionError("signature resolved with no log_args")

        monkeypatch.setattr(decorators.inspect, "signature", fail_signature)

        @log_operation(event_prefix="bare")
        async def op(name) -> str:
            """Return the name it was given.

            Args:
                name: The name to return.

            Returns:
                The name.
            """
            return name

        with caplog.at_level(logging.INFO, logger="brewery.core.decorators"):
            assert await op(name="foo") == "foo"

        messages = [r.getMessage() for r in caplog.records]
        assert any("bare_start" in m for m in messages)

        # Nothing was named, so no argument context is logged
        assert not any("name=foo" in m for m in messages)

    async def test_log_result_counts_sized_results(self, caplog) -> None:
        """Test that log_operation logs result counts for sized results."""

        import logging

        @log_operation(event_prefix="listop", log_result=True)
        async def op() -> list[int]:
            """Simulate an operation that may fail.

            Returns:
                The list of integers.
            """
            return [1, 2, 3]

        with caplog.at_level(logging.INFO, logger="brewery.core.decorators"):
            await op()

        assert any("count=3" in r.getMessage() for r in caplog.records)
