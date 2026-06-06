"""Homebrew Cask provider."""

from __future__ import annotations

from types import SimpleNamespace

from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.shell import run_brew_command

log: BreweryLogger = get_logger(name=__name__)


async def install(names: list[str]) -> list[str]:
    """Install Homebrew casks by name.

    Args:
        names: Name(s) of the cask(s) to install.

    Returns:
        The cask name(s) on success.

    Raises:
        BrewCommandError: If the installation fails.
    """
    await run_brew_command(subcommand="install", names=names, flags=["--cask"])

    return names


async def uninstall(names: list[str]) -> list[str]:
    """Uninstall Homebrew casks by name.

    Args:
        names: Name(s) of the cask(s) to uninstall.

    Returns:
        The cask name(s) on success.

    Raises:
        BrewCommandError: If the uninstallation fails.
    """
    await run_brew_command(subcommand="uninstall", names=names, flags=["--cask"])

    return names


async def upgrade(names: list[str]) -> list[str]:
    """Upgrade Homebrew casks by name.

    Args:
        names: Name(s) of the cask(s) to upgrade.

    Returns:
        The cask name(s) on success.

    Raises:
        BrewCommandError: If the upgrade fails.
        PinnedPackageWarning: If the package is pinned.
    """
    await run_brew_command(subcommand="upgrade", names=names, flags=[])

    return names


backend = SimpleNamespace(install=install, uninstall=uninstall, upgrade=upgrade)
