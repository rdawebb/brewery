"""Shell command execution utilities."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Sequence


class ShellError(RuntimeError):
    """Custom error for shell command failures."""

    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"Command failed ({returncode}): {' '.join(command)}\n{stderr}"
        )
        self.command = command
        self.returncode = returncode
        self.stderr = stderr

async def run_json(command: Sequence[str]) -> str:
    """Run a shell command and return stdout text.

    Args:
        command (Sequence[str]): The command to run.

    Returns:
        str: The stdout text from the command.

    Raises:
        ShellError: If non-zero exit code is returned.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise ShellError(list(command), process.returncode or -1, stderr.decode())

    return stdout.decode()
    
async def stream(command: Sequence[str]) -> AsyncIterator[str]:
    """Run a shell command and yield stdout lines as they are produced.

    Args:
        command (Sequence[str]): The command to run.
            
    Yields:
        str: Lines of stdout from the command.

    Raises:
        ShellError: If non-zero exit code is returned.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )

    assert process.stdout is not None
    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            yield line.decode(errors='replace').rstrip()
    finally:
        await process.wait()
        if process.returncode != 0:
            raise ShellError(list(command), process.returncode or -1, "")