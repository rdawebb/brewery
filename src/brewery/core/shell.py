"""Asynchronous shell command execution with timeout and JSON parsing."""

from __future__ import annotations

import asyncio
import json
import time
from asyncio.subprocess import Process
from typing import Any, Literal, Optional

from structlog.typing import FilteringBoundLogger

from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewCommandError,
    BrewTimeoutError,
    retry_on_transient,
)
from brewery.core.logging import get_logger

log: FilteringBoundLogger = get_logger(name=__name__)

ENV_OVERRIDES: dict[str, str] = {
    "LANG": "C",
    "HOMEBREW_NO_COLOR": "1",
}


async def run_brew_command(
    subcommand: Literal["install", "uninstall"],
    name: str,
    flags: list[str],
    timeout: int = 120,
) -> tuple[str, str, int]:
    """Run a Homebrew command asynchronously with optional timeout.

    Args:
        subcommand: The Homebrew subcommand to run (install or uninstall).
        name: The name of the formula or cask to operate on.
        flags: Additional flags to pass (e.g. `--formula`, `--cask`).
        timeout: Timeout in seconds (default: 120).

    Returns:
        A tuple of (stdout, stderr, returncode).

    Raises:
        BrewCommandError: If the command fails.
        BrewTimeoutError: If the command times out.
    """
    cmd: list[str] = ["brew", subcommand, *flags, name]
    log.info(
        event="brew_command_start", subcommand=subcommand, package=name, flags=flags
    )
    start: int | float = time.perf_counter()

    out, err, code = await run_capture(*cmd, timeout=timeout)
    duration_ms = int((time.perf_counter() - start) * 1000)

    if (
        code != 0
        and subcommand == "install"
        and "already installed" in (err + out).lower()
    ):
        raise AlreadyInstalledWarning(package=name)

    if code != 0:
        log.error(
            event="brew_command_failed",
            subcommand=subcommand,
            package=name,
            flags=flags,
            error=err or out,
            returncode=code,
            duration_ms=duration_ms,
        )
        raise BrewCommandError(command=" ".join(cmd), returncode=code, error=err or out)

    log.info(
        event="brew_command_success",
        subcommand=subcommand,
        package=name,
        flags=flags,
        duration_ms=duration_ms,
    )

    return out, err, code


async def run_capture(*cmd: str, timeout: Optional[int] = 30) -> tuple[str, str, int]:
    """Run a shell command asynchronously with optional timeout

    Args:
        *cmd: Command and its arguments to run.
        timeout: Timeout in seconds.

    Returns:
        A tuple of (stdout, stderr, returncode).

    Raises:
        BrewTimeoutError: If the command times out.
    """
    start: int | float = time.perf_counter()
    log.debug(event="command_start", command=" ".join(cmd), timeout=timeout)

    process: Process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    try:
        out, err = await asyncio.wait_for(fut=process.communicate(), timeout=timeout)
        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="command_complete",
            command=" ".join(cmd),
            returncode=process.returncode,
            duration_ms=duration_ms,
        )

    except asyncio.TimeoutError as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        log.error(
            event="command_timeout",
            command=" ".join(cmd),
            timeout=timeout,
            duration_ms=duration_ms,
        )
        try:
            process.kill()
        finally:
            raise BrewTimeoutError(command=" ".join(cmd), timeout=timeout) from e

    return (
        out.decode().strip(),
        err.decode().strip(),
        int(process.returncode) if process.returncode is not None else -1,
    )


@retry_on_transient(max_retries=3, base_delay=1.0)
async def run_json(*cmd: str, timeout: Optional[int] = 30) -> Any:
    """Run a shell command and parse its JSON output.

    Automatically retries on transient errors.

    Args:
        *cmd: Command and its arguments to run.
        timeout: Timeout in seconds.

    Returns:
        Parsed JSON output.

    Raises:
        BrewCommandError: If the command fails or JSON parsing fails.
        BrewTimeoutError: If the command times out (retried automatically).
    """
    start: int | float = time.perf_counter()
    out, err, code = await run_capture(*cmd, timeout=timeout)
    duration_ms = int((time.perf_counter() - start) * 1000)

    if code != 0:
        log.error(
            event="command_failed",
            command=" ".join(cmd),
            error=err or out,
            returncode=code,
        )
        raise BrewCommandError(command=" ".join(cmd), returncode=code, error=err or out)

    try:
        result: Any = json.loads(out)
        log.debug(event="json_parsed", command=" ".join(cmd), duration_ms=duration_ms)

        return result

    except json.JSONDecodeError as e:
        log.error(
            event="json_parse_failed",
            command=" ".join(cmd),
            error=str(object=e),
            exc_info=True,
        )
        raise BrewCommandError(
            message="Failed to parse JSON output",
            command=" ".join(cmd),
            error=out[:200] if out else "",
            context={"json_error": str(object=e)},
        ) from e
