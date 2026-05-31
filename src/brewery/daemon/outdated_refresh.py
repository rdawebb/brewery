"""Invoked by launchd every 30 minutes to keep the outdated cache warm."""

import asyncio

from brewery.core.logging import configure_logging
from brewery.core.repo import Repository


async def background_refresh() -> None:
    """Refresh the outdated cache by fetching the latest data from Homebrew."""
    repo = Repository()
    await repo.get_outdated(live=True)


if __name__ == "__main__":
    configure_logging(level="INFO")
    asyncio.run(background_refresh())
