"""Pin and unpin commands: hold a formula at its current version."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app
from brewery.cli.error_formatting import CommandFailed, command_error
from brewery.cli.output import (
    print_advisories,
    print_failures,
    print_result,
)
from brewery.core.errors import UserError


def _reject_casks(cask: bool, command: str, names: list[str]) -> None:
    """Refuse a cask-scoped invocation with a pointer at brew.

    Brewery tracks pin state for formulae only; brew keeps pinned casks in a
    separate bookkeeping directory that the scanner does not read.

    Args:
        cask: Whether --cask was passed.
        command: The command name, for the suggestion.
        names: The named packages, for the suggestion.

    Raises:
        UserError: Always, when `cask` is set.
    """
    if cask:
        raise UserError(
            f"Pinning casks is not supported yet\n"
            f"   Suggestion: Try 'brew {command} --cask {' '.join(names)}'"
        )


def _report(
    verb: str,
    done: list[str],
    advisories: list[tuple[str, str]],
    failures: list[tuple[str, str]],
) -> None:
    """Print the advisory, success, and failure blocks of a pin/unpin run.

    Args:
        verb: The infinitive verb, e.g. "pin".
        done: Names the command acted on.
        advisories: (name, reason) pairs that warn but do not fail.
        failures: (name, reason) pairs that set the exit code.
    """
    print_advisories(advisories)

    if done or not failures:
        print_result(
            f"✓ {verb.capitalize()}ned {len(done)} package(s)\n",
            done,
            style="bold green",
        )

    print_failures(f"✗ Failed to {verb} {len(failures)} package(s)", failures)


@app.command(aliases=["p"])
@command_error()
def pin(
    names: list[str],
    cask: bool = app.Option(False, "--cask", help="Not supported - use brew"),
) -> None:
    """Pin a formula, preventing it from being upgraded.

    Args:
        names: Name(s) of the formula(e) to pin.
        cask: Rejected; brewery pins formulae only.
    """
    _reject_casks(cask=cask, command="pin", names=names)

    with _repository() as repo:
        sys.stdout.write("\n")
        pinned, advisories, failures = repo.pin_packages(names)

        _report("pin", pinned, advisories, failures)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed


@app.command(aliases=["unp"])
@command_error()
def unpin(
    names: list[str],
    cask: bool = app.Option(False, "--cask", help="Not supported - use brew"),
) -> None:
    """Unpin a formula, allowing it to be upgraded again.

    Args:
        names: Name(s) of the formula(e) to unpin.
        cask: Rejected; brewery pins formulae only.
    """
    _reject_casks(cask=cask, command="unpin", names=names)

    with _repository() as repo:
        sys.stdout.write("\n")
        unpinned, advisories, failures = repo.unpin_packages(names)

        _report("unpin", unpinned, advisories, failures)

        sys.stdout.write("\n")
        if failures:
            raise CommandFailed
