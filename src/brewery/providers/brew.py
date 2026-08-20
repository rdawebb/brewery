"""Homebrew fallback package backends (formula + cask)."""

from __future__ import annotations

from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewCommandError,
    PinnedPackageWarning,
)
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.shell import BrewOutput, BrewResult, run_brew
from brewery.providers.base import PackageBackend

log: BreweryLogger = get_logger(name=__name__)


def _raise_for_known(subcommand: str, names: list[str], result: BrewResult) -> None:
    """Map brew's human-readable failure messages to typed warnings.

    Only consulted on a non-zero exit. Message matching is intentionally loose
    and may need updating if brew changes its wording.

    Args:
        subcommand: The brew subcommand that was run (e.g. "install").
        names: The package names passed to the subcommand.
        result: The captured output and return code from brew.
    """
    combined = (result.stderr + result.stdout).lower()

    if subcommand == "install" and "already installed" in combined:
        matched = [n for n in names if n in combined] or names
        raise AlreadyInstalledWarning(package=", ".join(matched))

    if subcommand == "upgrade" and "pinned" in combined:
        pinned = [n for n in names if n in combined] or names
        raise PinnedPackageWarning(package=", ".join(pinned))


async def _run(subcommand: str, names: list[str], flags: list[str]) -> list[str]:
    """Run a brew package subcommand, capturing output for semantic mapping.

    Args:
        subcommand: The brew subcommand to run (e.g. "install").
        names: The package names to pass to the subcommand.
        flags: Extra flags to insert between the subcommand and the names.

    Returns:
        The same list of names on success.
    """
    result = await run_brew(
        [subcommand, *flags, *names], output=BrewOutput.CAPTURE, check=False
    )

    if result.returncode != 0:
        _raise_for_known(subcommand, names, result)  # may raise a typed warning
        raise BrewCommandError(
            command=f"brew {subcommand} {' '.join(flags + names)}",
            returncode=result.returncode,
            error=result.stderr or result.stdout,
        )

    return names


class BrewBackend:  # Implements base.PackageBackend
    """A brew-backed package backend for one package kind.

    install/uninstall carry the kind flag; upgrade takes none (brew infers it),
    matching the original providers.
    """

    def __init__(self, kind_flag: str) -> None:
        """Initialise the backend.

        Args:
            kind_flag: The brew flag selecting the kind, e.g. `"--formula"`.
        """
        self._kind_flag = kind_flag

    async def install(self, names: list[str]) -> list[str]:
        """Install packages by name.

        Args:
            names: Package names to install.

        Returns:
            The same list of names on success.
        """
        return await _run("install", names, [self._kind_flag])

    async def uninstall(self, names: list[str]) -> list[str]:
        """Uninstall packages by name.

        Args:
            names: Package names to uninstall.

        Returns:
            The same list of names on success.
        """
        return await _run("uninstall", names, [self._kind_flag])

    async def upgrade(self, names: list[str]) -> list[str]:
        """Upgrade packages by name.

        Args:
            names: Package names to upgrade.

        Returns:
            The same list of names on success.
        """
        return await _run("upgrade", names, [])


# Static assertion that the backend satisfies the protocol
_package_backend: type[PackageBackend] = BrewBackend

formula_backend = BrewBackend("--formula")
cask_backend = BrewBackend("--cask")
