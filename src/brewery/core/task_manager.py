"""Batch operation orchestration with concurrency control and error handling."""

from __future__ import annotations

import asyncio
from asyncio.tasks import Task
from typing import Awaitable, Callable, Generic, TypeVar

from structlog.typing import FilteringBoundLogger

from brewery.core.logging import get_logger

log: FilteringBoundLogger = get_logger(name=__name__)

T = TypeVar(name="T")


class BatchResult(Generic[T]):
    """Result of a batch operation."""

    def __init__(self):
        self.successes: list[T] = []
        self.failures: list[tuple[str, str]] = []  # (item_id, error_msg)

    def add_success(self, item: T) -> None:
        """Add a successful operation result."""
        self.successes.append(item)

    def add_failure(self, item_id: str, error: str) -> None:
        """Add a failed operation result."""
        self.failures.append((item_id, error))

    def is_successful(self) -> bool:
        """Return True if all operations succeeded."""
        return len(self.failures) == 0

    def summary(self) -> str:
        """Return a summary string for logging."""
        return f"{len(self.successes)} succeeded, {len(self.failures)} failed"


class BatchOperationManager:
    """Orchestrates batch operations with concurrency control and error collection."""

    @staticmethod
    async def execute_many(
        items: list[str],
        operation: Callable[[str], Awaitable[T]],
        operation_name: str = "operation",
        max_concurrent: int = 5,
        on_error: Callable[[str, Exception], str] | None = None,
    ) -> BatchResult[T]:
        """Execute an operation on multiple items with concurrency control.

        Args:
            items: List of item identifiers to process.
            operation: Async function taking item name and returning result T.
            operation_name: Human-readable name for logging.
            max_concurrent: Maximum concurrent operations (default: 5).
            on_error: Optional function to convert exceptions to error messages.
                     Signature: (item_name: str, exception: Exception) -> str

        Returns:
            BatchResult[T] with successes and failures separated.
        """
        result: BatchResult = BatchResult[T]()

        log.info(
            event="batch_operation_start",
            operation=operation_name,
            count=len(items),
            max_concurrent=max_concurrent,
        )

        if not items:
            log.info(
                event="batch_operation_complete",
                operation=operation_name,
                summary="0 items",
            )
            return result

        # Create semaphore to limit concurrency
        semaphore = asyncio.Semaphore(value=max_concurrent)

        async def bounded_operation(item: str) -> tuple[str, T | None, str | None]:
            """Run operation with concurrency limit and error handling."""
            async with semaphore:
                try:
                    result = await operation(item)
                    log.debug(
                        event="batch_item_success",
                        operation=operation_name,
                        item=item,
                    )
                    return (item, result, None)
                except Exception as e:
                    error_msg: str = on_error(item, e) if on_error else str(object=e)
                    log.warning(
                        event="batch_item_failed",
                        operation=operation_name,
                        item=item,
                        error=error_msg,
                    )
                    return (item, None, error_msg)

        # Run all operations concurrently (no early exits)
        tasks: list = [bounded_operation(item) for item in items]
        outcomes: list = await asyncio.gather(*tasks, return_exceptions=False)

        # Collect results
        for item_id, success_result, error_msg in outcomes:
            if error_msg:
                result.add_failure(item_id, error=error_msg)
            else:
                result.add_success(item=success_result)

        log.info(
            event="batch_operation_complete",
            operation=operation_name,
            summary=result.summary(),
        )

        return result

    @staticmethod
    async def execute_many_typed(
        items: dict[str, T],
        operation: Callable[[str, T], Awaitable[T]],
        operation_name: str = "operation",
        max_concurrent: int = 5,
        on_error: Callable[[str, Exception], str] | None = None,
    ) -> BatchResult[tuple[str, T]]:
        """Execute operation on items with associated values, for operations with metadata.

        Args:
            items: Dict mapping item ID to item data.
            operation: Async function taking (item_id, item_data) and returning result.
            operation_name: Human-readable name for logging.
            max_concurrent: Maximum concurrent operations.
            on_error: Optional error formatter.

        Returns:
            BatchResult with (item_id, result) tuples as successes.
        """
        result: BatchResult = BatchResult[tuple[str, T]]()

        log.info(
            event="batch_operation_typed_start",
            operation=operation_name,
            count=len(items),
        )

        semaphore = asyncio.Semaphore(value=max_concurrent)

        async def bounded_operation(
            item_id: str, item_data: T
        ) -> tuple[str, tuple[str, T] | None, str | None]:
            """Run operation with data and error handling."""
            async with semaphore:
                try:
                    result = await operation(item_id, item_data)
                    log.debug(
                        event="batch_item_success",
                        operation=operation_name,
                        item=item_id,
                    )
                    return (item_id, (item_id, result), None)
                except Exception as e:
                    error_msg: str = on_error(item_id, e) if on_error else str(object=e)
                    log.warning(
                        event="batch_item_failed",
                        operation=operation_name,
                        item=item_id,
                        error=error_msg,
                    )
                    return (item_id, None, error_msg)

        tasks: list = [
            bounded_operation(item_id, item_data)
            for item_id, item_data in items.items()
        ]
        outcomes: list = await asyncio.gather(*tasks, return_exceptions=False)

        for item_id, success_result, error_msg in outcomes:
            if error_msg:
                result.add_failure(item_id, error=error_msg)
            else:
                result.add_success(item=success_result)

        log.info(
            event="batch_operation_typed_complete",
            operation=operation_name,
            summary=result.summary(),
        )

        return result


# Keep the old TaskManager for background task queuing (still needed)
class BackgroundTaskManager:
    """Manages background tasks and job queues."""

    def __init__(self) -> None:
        """Initialise the BackgroundTaskManager."""
        self._tasks: set[asyncio.Task] = set()

    def add_task(self, coro) -> asyncio.Task:
        """Add a new coroutine as a background task."""
        task: Task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._remove_task)
        log.debug(event="task_added", task_count=len(self._tasks))
        return task

    def _remove_task(self, task: asyncio.Task) -> None:
        """Remove a completed task from the manager."""
        self._tasks.discard(task)
        log.debug(event="task_removed", task_count=len(self._tasks))

    async def wait_for_all(self) -> None:
        """Wait for all pending tasks to complete."""
        pending: list = [task for task in self._tasks if not task.done()]
        if pending:
            log.info(event="waiting_for_background_tasks", count=len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
            log.info(event="background_tasks_completed")

    def clear(self) -> None:
        """Clear all completed tasks."""
        self._tasks.clear()
        log.debug(event="task_manager_cleared")


# Global instances
_batch_op_manager: BatchOperationManager | None = None
_bg_task_manager: BackgroundTaskManager | None = None


def get_batch_operation_manager() -> BatchOperationManager:
    """Get the global BatchOperationManager instance."""
    global _batch_op_manager
    if _batch_op_manager is None:
        _batch_op_manager = BatchOperationManager()
        log.debug(event="batch_operation_manager_created")
    return _batch_op_manager


def get_task_manager() -> BackgroundTaskManager:
    """Get the global BackgroundTaskManager instance."""
    global _bg_task_manager
    if _bg_task_manager is None:
        _bg_task_manager = BackgroundTaskManager()
        log.debug(event="background_task_manager_created")
    return _bg_task_manager
