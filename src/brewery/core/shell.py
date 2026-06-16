"""Asynchronous shell command execution with timeout and JSON parsing."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from enum import Enum

from brewery.core.decorators import retry_on_transient
from brewery.core.errors import (
    BrewCommandError,
    BrewTimeoutError,
)
from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

ENV_OVERRIDES: dict[str, str] = {
    "LANG": "C",
    "HOMEBREW_NO_COLOR": "1",
}


class BrewOutput(Enum):
    CAPTURE = "capture"  # Pipe + return text
    INHERIT = "inherit"  # Stream to the terminal (interactive)


@dataclass(frozen=True)
class BrewResult:
    stdout: str  # "" in INHERIT mode
    stderr: str  # "" in INHERIT mode
    returncode: int


@retry_on_transient()
async def run_brew(
    args: list[str],
    *,
    output: BrewOutput = BrewOutput.CAPTURE,
    timeout: float | None = None,
    check: bool = True,
) -> BrewResult:
    """Run `brew <args>` asynchronously.

    Args:
        args: Arguments after `brew` (e.g. `["install", "--formula", "wget"]`).
        output: CAPTURE to pipe and return text; INHERIT to stream to the terminal.
        timeout: Seconds before the process is killed (None = no limit). Leave
            None for interactive INHERIT runs and long downloads.
        check: If True, a non-zero exit raises BrewCommandError. Set False when
            the caller wants to inspect the result first (e.g. to distinguish
            "already installed" from a real failure).

    Returns:
        BrewResult(stdout, stderr, returncode). stdout/stderr are empty in
        INHERIT mode.

    Raises:
        BrewCommandError: brew missing, or non-zero exit with check=True.
        BrewTimeoutError: the command exceeded timeout.
    """
    if shutil.which("brew") is None:
        raise BrewCommandError(
            command="brew " + " ".join(args),
            returncode=127,
            error="brew not found on PATH",
        )

    cmd = ["brew", *args]
    capture = output is BrewOutput.CAPTURE

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE if capture else None,
        stderr=asyncio.subprocess.PIPE if capture else None,
    )

    try:
        if capture:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
            out, err = out_b.decode(), err_b.decode()

        else:
            await asyncio.wait_for(proc.wait(), timeout)
            out, err = "", ""

    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        await proc.wait()
        raise BrewTimeoutError(command=" ".join(cmd), timeout=timeout)

    code = proc.returncode or 0

    if check and code != 0:
        log.error(
            event="brew_command_failed",
            args=args,
            error=(err or out),
            returncode=code,
        )
        raise BrewCommandError(
            command=" ".join(cmd),
            returncode=code,
            error=(err or out),
        )

    log.info(event="brew_command_success", args=args, returncode=code)

    return BrewResult(stdout=out, stderr=err, returncode=code)
