"""Asynchronous shell command execution with timeout and JSON parsing."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from enum import Enum

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

# How long a signalled brew gets to release its lock and clear its keg before
# it is killed outright
_GRACE = 2.0

# Keeps one brew failure to one greppable log line; the untruncated text still
# reaches the caller on `BrewResult` and `BrewCommandError`
_MAX_LOGGED_ERROR = 500


class BrewOutput(Enum):
    CAPTURE = "capture"  # Pipe + return text
    INHERIT = "inherit"  # Stream to the terminal (interactive)


@dataclass(frozen=True)
class BrewResult:
    stdout: str  # "" in INHERIT mode
    stderr: str  # "" in INHERIT mode
    returncode: int


def _one_line(text: str) -> str:
    """Collapse brew's output to a single bounded line for logging.

    brew reports failures over several lines; embedded newlines would break the
    one-event-per-line log shape.

    Args:
        text: The captured output from a failed brew run.

    Returns:
        The text on one line, truncated past `_MAX_LOGGED_ERROR`.
    """
    flat = " ".join(text.split())

    return flat if len(flat) <= _MAX_LOGGED_ERROR else f"{flat[:_MAX_LOGGED_ERROR]}..."


def _signal_brew(proc: asyncio.subprocess.Process, sig: int, *, detached: bool) -> bool:
    """Signal a brew process, or its whole process group when detached.

    brew spawns the real work (build.rb, cmake, the compiler) as children, so
    signalling `proc` alone leaves them running.

    Args:
        proc: The brew process.
        sig: The signal to send.
        detached: Whether the process was started with its own session.

    Returns:
        True if the signal was delivered, False if the process was already gone.
    """
    try:
        if detached:
            os.killpg(os.getpgid(proc.pid), sig)

        else:
            proc.send_signal(sig)

    except (ProcessLookupError, PermissionError, OSError):
        return False

    return True


async def _terminate_brew(proc: asyncio.subprocess.Process, *, detached: bool) -> None:
    """Stop a brew process and everything it spawned, then reap it.

    Detached runs never see the terminal's Ctrl-C, so without this brew keeps
    building after brewery exits; SIGINT first, since brew handles it by
    releasing its lock and clearing the partial keg; SIGKILL after `_GRACE`.

    Args:
        proc: The brew process.
        detached: Whether the process was started with its own session.
    """
    if proc.returncode is not None:
        return

    if not _signal_brew(proc, signal.SIGINT, detached=detached):
        return

    # Shielded: the wait outlives this timeout so the second `wait` can reuse it
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(asyncio.shield(proc.wait()), _GRACE)

    if proc.returncode is None:
        _signal_brew(proc, signal.SIGKILL, detached=detached)

    with contextlib.suppress(asyncio.CancelledError):
        await proc.wait()


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
    cmd = ["brew", *args]
    capture = output is BrewOutput.CAPTURE

    # Only force LANG=C / no-color when BrewOutput.CAPTURE is in use
    env = {**os.environ, **ENV_OVERRIDES} if capture else None

    # The exec is the brew-on-PATH check
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            # Detach from the terminal unless INHERIT mode is used to prevent
            # progress display interference
            stdin=asyncio.subprocess.DEVNULL if capture else None,
            start_new_session=capture,
            stdout=asyncio.subprocess.PIPE if capture else None,
            stderr=asyncio.subprocess.PIPE if capture else None,
            env=env,
        )

    except FileNotFoundError:
        raise BrewCommandError(
            command=" ".join(cmd),
            returncode=127,
            error="brew not found on PATH",
        ) from None

    try:
        if capture:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout)
            out, err = out_b.decode(), err_b.decode()

        else:
            await asyncio.wait_for(proc.wait(), timeout)
            out, err = "", ""

    except TimeoutError:
        await _terminate_brew(proc, detached=capture)
        raise BrewTimeoutError(command=" ".join(cmd), timeout=timeout)

    # Ctrl+C terminates any brew processes as well
    except asyncio.CancelledError:
        log.info(event="brew_command_cancelled", args=args)
        await _terminate_brew(proc, detached=capture)
        raise

    code = proc.returncode or 0

    if code != 0:
        # Logged even when check=False, as it's then the only record of brew's error
        emit = log.error if check else log.warning
        emit(
            event="brew_command_failed",
            args=args,
            returncode=code,
            error=_one_line(err or out),
        )

        if check:
            raise BrewCommandError(
                command=" ".join(cmd),
                returncode=code,
                error=(err or out),
            )

    else:
        log.info(event="brew_command_success", args=args, returncode=code)

    return BrewResult(stdout=out, stderr=err, returncode=code)
