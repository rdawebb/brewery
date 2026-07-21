"""Background daemon service management, dispatched by host platform.

The CLI uses this package: launchd on macOS, systemd (user units) on Linux.
Both backends expose the same interface: `start`, `stop`, `is_running`,
and a `SERVICE_LABEL` constant.
"""

from __future__ import annotations

import platform

from brewery.daemon import launchd, systemd

_backend = systemd if platform.system() == "Linux" else launchd

SERVICE_LABEL: str = _backend.SERVICE_LABEL
start = _backend.start
stop = _backend.stop
is_running = _backend.is_running

__all__ = ["SERVICE_LABEL", "is_running", "start", "stop"]
