"""Common Homebrew backend functions."""

from __future__ import annotations

import json
import os
from typing import Any

from brewery.core.shell import run_json


def brew_env() -> dict[str, str]:
    """Get the Homebrew environment variables.

    Returns:
        dict[str, str]: A dictionary of Homebrew environment variables.
    """
    env = os.environ.copy()

    return env

async def brew_json(args: list[str]) -> Any:
    """Run a Homebrew command and return the parsed JSON output.

    Args:
        args (list[str]): The arguments to pass to the `brew` command.
        
    Returns:
        Any: The parsed JSON output from the command.
    """
    text = await run_json(["brew", *args])

    return json.loads(text)

def human_size_from_bytes(maybe_bytes: int | None) -> str:
    """Convert a size in bytes to a human-readable string.

    Args:
        maybe_bytes (int | None): The size in bytes.

    Returns:
        str: The human-readable size string.
    """
    if not maybe_bytes:
        return "-"
    
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(maybe_bytes)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1

    return f"{size:.2f} {units[i]}"