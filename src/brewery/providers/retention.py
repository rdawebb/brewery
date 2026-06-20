"""Brewery-local keg retention metadata."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPLACED_SIDECAR = ".brewery_replaced.json"


def mark_replaced(keg: Path, *, by: str | None = None, at: int | None = None) -> None:
    """Stamp a superseded keg with the time it was replaced.

    Written at keg root, sharing the keg's lifecycle. Records only the
    supersession time (and optionally the replacing version). Source of
    truth for cleanup retention.

    Args:
        keg: The old keg now retained as a stale version.
        by: Optional version that replaced it (for rollback).
        at: Unix epoch seconds, defaults to now.
    """
    payload: dict = {"replaced_at": at if at is not None else int(time.time())}
    if by is not None:
        payload["replaced_by"] = by

    sidecar = keg / REPLACED_SIDECAR
    tmp = sidecar.with_name(sidecar.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, sidecar)


def read_replaced(keg: Path) -> dict | None:
    """Read a keg's replaced sidecar, tolerating absence/corruption.

    Args:
        keg: The keg directory.

    Returns:
        The parsed sidecar, or None if missing/unreadable — cleanup treats a
        missing sidecar as not auto-eligible.
    """
    try:
        return json.loads((keg / REPLACED_SIDECAR).read_text())

    except (OSError, ValueError):
        return None
