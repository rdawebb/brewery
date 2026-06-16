"""Homebrew package backends (formula + cask).

Replaces the near-identical brew_formula.py and brew_cask.py: a single factory
builds both backends, differing only by the kind flag. The install/upgrade
"already installed" / "pinned" interpretation lives here -- it's package
semantics, not something the shell primitive should know.
"""

from __future__ import annotations

from types import SimpleNamespace

from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewCommandError,
    PinnedPackageWarning,
)
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.shell import BrewOutput, BrewResult, run_brew

log: BreweryLogger = get_logger(name=__name__)


def _raise_for_known(subcommand: str, names: list[str], result: BrewResult) -> None:
    """Map brew's human-readable failure messages to typed warnings.

    Only consulted on a non-zero exit. Message matching is intentionally loose
    and may need updating if brew changes its wording.

    Args:
        subcommand: The brew subcommand that was run (e.g. ``"install"``).
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
        subcommand: The brew subcommand to run (e.g. ``"install"``).
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


def _make_backend(kind_flag: str) -> SimpleNamespace:
    """Build a backend for a package kind. install/uninstall carry the kind flag;
    upgrade takes none (brew infers it), matching the original providers."""

    async def install(names: list[str]) -> list[str]:
        """Install packages by name.

        Args:
            names: Package names to install.

        Returns:
            The same list of names on success.
        """
        return await _run("install", names, [kind_flag])

    async def uninstall(names: list[str]) -> list[str]:
        """Uninstall packages by name.

        Args:
            names: Package names to uninstall.

        Returns:
            The same list of names on success.
        """
        return await _run("uninstall", names, [kind_flag])

    async def upgrade(names: list[str]) -> list[str]:
        """Upgrade packages by name.

        Args:
            names: Package names to upgrade.

        Returns:
            The same list of names on success.
        """
        return await _run("upgrade", names, [])

    return SimpleNamespace(install=install, uninstall=uninstall, upgrade=upgrade)


formula_backend = _make_backend("--formula")
cask_backend = _make_backend("--cask")
