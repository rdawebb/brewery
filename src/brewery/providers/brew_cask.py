"""Homebrew Cask provider."""

from __future__ import annotations

from typing import Any

from structlog.typing import FilteringBoundLogger

from brewery.core.decorators import log_operation
from brewery.core.errors import PackageNotFoundError
from brewery.core.logging import get_logger
from brewery.core.models import Package, PackageKind
from brewery.core.shell import run_brew_command, run_capture, run_json
from brewery.providers.package_builder import build_packages_batch

log: FilteringBoundLogger = get_logger(name=__name__)

BATCH_SIZE = 30


async def _get_caskroom_path() -> str:
    """Get the caskroom path.

    Return:
        String representation of the caskroom path.
    """
    caskroom_out, _, caskroom_code = await run_capture("brew", "--caskroom")
    caskroom_path: str = (
        caskroom_out.strip() if caskroom_code == 0 else "/usr/local/Caskroom"
    )

    return caskroom_path


@log_operation(event_prefix="list_installed_casks")
async def list_installed() -> list[Package]:
    """List installed Homebrew casks.

    Returns:
        A list of installed Package instances.
    """
    out, _, _ = await run_capture("brew", "list", "--cask")
    names: list[str] = [name.strip() for name in out.split(sep="\n") if name.strip()]

    caskroom_path: str = await _get_caskroom_path()

    pkgs: list[Package] = []
    for i in range(0, len(names), BATCH_SIZE):
        batch: list[str] = names[i : i + BATCH_SIZE]
        data: Any = await run_json("brew", "info", "--json=v2", "--cask", *batch)
        items: Any = data.get("casks", [])

        batch_pkgs: list[Package] = await build_packages_batch(
            items=items, kind=PackageKind.CASK, caskroom_path=caskroom_path
        )

        pkgs.extend(batch_pkgs)

    return pkgs


@log_operation(event_prefix="cask_package_info", log_args=["names"])
async def info(names: list[str]) -> list[Package]:
    """Get cask info by name(s).

    Args:
        names: Name(s) of the cask(s).

    Returns:
        Package instance(s) with detailed information.
    """
    if not names:
        return []

    caskroom_path: str = await _get_caskroom_path()

    pkgs: list[Package] = []
    for i in range(0, len(names), BATCH_SIZE):
        batch: list[str] = names[i : i + BATCH_SIZE]
        data: Any = await run_json("brew", "info", "--json=v2", "--cask", *batch)
        items = data.get("casks", [])

        if not items and i == 0 and len(names) == 1:
            raise PackageNotFoundError(package=names[0], kind="cask")

        batch_pkgs: list[Package] = await build_packages_batch(
            items=items, kind=PackageKind.CASK, caskroom_path=caskroom_path
        )
        pkgs.extend(batch_pkgs)

    return pkgs


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
