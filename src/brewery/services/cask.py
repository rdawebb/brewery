"""Cask operations, handed to brew wholesale.

Casks have no native path yet and are passed to brew as a batch.
"""

from __future__ import annotations

from brewery.providers.base import InstallBackend, UninstallBackend, UpgradeBackend


async def install_casks(names: list[str], *, backend: InstallBackend) -> None:
    """Install every named cask.

    Args:
        names: Cask tokens to install.
        backend: The cask backend to delegate to.
    """
    await backend.install(names=names)


async def uninstall_casks(names: list[str], *, backend: UninstallBackend) -> None:
    """Uninstall every named cask.

    Args:
        names: Cask tokens to uninstall.
        backend: The cask backend to delegate to.
    """
    await backend.uninstall(names=names)


async def upgrade_casks(names: list[str], *, backend: UpgradeBackend) -> None:
    """Upgrade every named cask.

    Args:
        names: Cask tokens to upgrade.
        backend: The cask backend to delegate to.
    """
    await backend.upgrade(names=names)
