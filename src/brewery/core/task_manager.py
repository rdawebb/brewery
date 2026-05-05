"""Task manager for handling background tasks and job queues."""

from __future__ import annotations

import asyncio
from _asyncio import Task
from typing import TYPE_CHECKING, Set

from structlog.typing import FilteringBoundLogger

if TYPE_CHECKING:
    from ty_extensions import Unknown

from brewery.core.logging import get_logger

log: FilteringBoundLogger = get_logger(name=__name__)


class TaskManager:
    """Manages background tasks and job queues."""

    def __init__(self) -> None:
        """Initialise the TaskManager."""
        self._tasks: Set[asyncio.Task] = set()

    def add_task(self, coro) -> asyncio.Task:
        """Add a new coroutine background task.

        Args:
            coro: The coroutine to run as a background task.

        Returns:
            The created task.
        """
        task: Task[Unknown] = asyncio.create_task(coro)
        self._tasks.add(task)

        task.add_done_callback(self._remove_task)

        log.debug(event="task_added", task_count=len(self._tasks))

        return task

    def _remove_task(self, task: asyncio.Task) -> None:
        """Remove a completed task from the manager.

        Args:
            task: The completed task to remove.
        """
        self._tasks.discard(task)
        log.debug(event="task_removed", task_count=len(self._tasks))

    def get_pending_tasks(self) -> list[asyncio.Task]:
        """Get a list of pending tasks.

        Returns:
            A list of pending tasks.
        """
        pending: list[Task[Unknown]] = [task for task in self._tasks if not task.done()]
        log.debug(event="get_pending_tasks", count=len(pending))

        return pending

    async def wait_for_all(self) -> None:
        """Wait for all pending tasks to complete.

        Returns exceptions without raising.
        """
        pending: list[Task[Unknown]] = self.get_pending_tasks()

        if pending:
            log.info(event="waiting_for_background_tasks", count=len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
            log.info(event="background_tasks_completed")

    def clear(self) -> None:
        """Clear all completed tasks from the manager."""
        self._tasks.clear()
        log.debug(event="task_manager_cleared")


# Global TaskManager instance
_task_manager: TaskManager | None = None


def get_task_manager() -> TaskManager:
    """Get the global TaskManager instance, creating it if necessary.

    Returns:
        The global TaskManager instance.
    """
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
        log.debug(event="task_manager_created")

    return _task_manager
