"""Asynchronous shell command execution with timeout and JSON parsing."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from brewery.core.errors import BrewCommandError, BrewTimeoutError, retry_on_transient
from brewery.core.logging import get_logger

log = get_logger(__name__)

ENV_OVERRIDES = {
    "LANG": "C",
    "HOMEBREW_NO_COLOR": "1",
}


async def run_capture(
    *cmd: str, timeout: Optional[int] = 30
) -> tuple[str, str, int]:
    """Run a shell command asynchronously with optional timeout
    
    Args:
        *cmd: Command and its arguments to run.
        timeout: Timeout in seconds.
        
    Returns:
        A tuple of (stdout, stderr, returncode).

    Raises:
        BrewTimeoutError: If the command times out.
    """
    start = time.perf_counter()
    log.debug("command_start", command=" ".join(cmd), timeout=timeout)

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "command_complete",
            command=" ".join(cmd),
            returncode=process.returncode,
            duration_ms=duration_ms
        )

    except asyncio.TimeoutError as e:
        duration_ms = int((time.perf_counter() - start) * 1000)
        log.error(
            "command_timeout",
            command=" ".join(cmd),
            timeout=timeout,
            duration_ms=duration_ms
        )
        try:
            process.kill()
        finally:
            raise BrewTimeoutError(
                f"Command timed out after {timeout}s",
                context={
                    "command": " ".join(cmd),
                    "timeout": timeout,
                    "duration_ms": duration_ms
                }
            ) from e
    
    return out.decode().strip(), err.decode().strip(), process.returncode

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
    start = time.perf_counter()
    out, err, code = await run_capture(*cmd, timeout=timeout)
    duration_ms = int((time.perf_counter() - start) * 1000)

    if code != 0:
        log.error(
            "command_failed",
            command=" ".join(cmd),
            error=err or out,
            returncode=code
        )
        raise BrewCommandError(
            f"Brew command failed with exit code {code}",
            context={
                "command": " ".join(cmd),
                "returncode": code,
                "error": err or out,
                "duration_ms": duration_ms
            }
        )
    
    try:
        result = json.loads(out)
        log.debug(
            "json_parsed",
            command=" ".join(cmd),
            duration_ms=duration_ms
        )

        return result
    
    except json.JSONDecodeError as e:
        log.error(
            "json_parse_failed",
            command=" ".join(cmd),
            error=str(e),
            exc_info=True
        )
        raise BrewCommandError(
            "Failed to parse JSON output",
            context={
                "command": " ".join(cmd),
                "error": str(e),
                "output_preview": out[:200] if out else ""
            }
        ) from e