"""Homebrew formula provider."""

from __future__ import annotations

from types import SimpleNamespace

from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.shell import run_brew_command

log: BreweryLogger = get_logger(name=__name__)


async def install(names: list[str]) -> list[str]:
    """Install Homebrew formulae by name.

    Args:
        names: Name(s) of the formulae to install.

    Returns:
        The package name(s) on success.

    Raises:
        BrewCommandError: If the installation fails.
    """
    await run_brew_command(subcommand="install", names=names, flags=["--formula"])

    return names


async def uninstall(names: list[str]) -> list[str]:
    """Uninstall Homebrew formulae by name.

    Args:
        names: Name(s) of the formulae to uninstall.

    Returns:
        The package name(s) on success.

    Raises:
        BrewCommandError: If the uninstallation fails.
    """
    await run_brew_command(subcommand="uninstall", names=names, flags=["--formula"])

    return names


async def upgrade(names: list[str]) -> list[str]:
    """Upgrade Homebrew formulae by name.

    Args:
        names: Name(s) of the formulae to upgrade.

    Returns:
        The package name(s) on success.

    Raises:
        BrewCommandError: If the upgrade fails.
        PinnedPackageWarning: If the package is pinned.
    """
    await run_brew_command(subcommand="upgrade", names=names, flags=[])

    return names


backend = SimpleNamespace(install=install, uninstall=uninstall, upgrade=upgrade)
