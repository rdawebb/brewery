"""Background task management."""

from __future__ import annotations

import asyncio
from asyncio.tasks import Task

from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)


class BackgroundTaskManager:
    """Manages background tasks and job queues."""

    def __init__(self) -> None:
        """Initialise the BackgroundTaskManager."""
        self._tasks: set[asyncio.Task] = set()

    def add_task(self, coro) -> asyncio.Task:
        """Add a new coroutine as a background task.

        Args:
            coro: The coroutine to add as a background task.

        Returns:
            The created task.
        """
        task: Task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._remove_task)
        log.debug(event="task_added", task_count=len(self._tasks))
        return task

    def _remove_task(self, task: asyncio.Task) -> None:
        """Remove a completed task from the manager.

        Args:
            task: The task to remove.
        """
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


# Global instance
_bg_task_manager: BackgroundTaskManager | None = None


def get_task_manager() -> BackgroundTaskManager:
    """Get the global BackgroundTaskManager instance.

    Returns:
        The global BackgroundTaskManager instance.
    """
    global _bg_task_manager
    if _bg_task_manager is None:
        _bg_task_manager = BackgroundTaskManager()
        log.debug(event="background_task_manager_created")

    return _bg_task_manager
