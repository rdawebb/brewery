"""Homebrew formula provider."""

from __future__ import annotations

from typing import Any

from brewery.core.decorators import log_operation
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import Package, PackageKind
from brewery.core.shell import run_brew_command, run_json
from brewery.providers.package_builder import batch_info, build_packages_batch

log: BreweryLogger = get_logger(name=__name__)


@log_operation(event_prefix="list_installed_formulae")
async def list_installed() -> list[Package]:
    """List installed Homebrew formulae.

    Returns:
        A list of installed Package instances.
    """
    data: dict[str, Any] = await run_json("brew", "info", "--json=v2", "--installed")
    items: list[dict[str, Any]] = data.get("formulae", [])

    pkgs: list[Package] = await build_packages_batch(
        items=items, kind=PackageKind.FORMULA
    )

    return pkgs


@log_operation(event_prefix="_formulae_package_info", log_args=["names"])
async def info(names: list[str]) -> list[Package]:
    """Get Homebrew formula info by name(s).

    Args:
        names: Name(s) of the formula.

    Returns:
        Package instance(s) with detailed information.
    """
    if not names:
        return []

    pkgs: list[Package] = await batch_info(
        names=names, flags=[], json_key="formulae", kind=PackageKind.FORMULA
    )

    return pkgs


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


class _Backend:
    """Backend for Homebrew formulae."""

    list_installed = staticmethod(list_installed)
    info = staticmethod(info)
    install = staticmethod(install)
    uninstall = staticmethod(uninstall)
    upgrade = staticmethod(upgrade)


backend = _Backend()
