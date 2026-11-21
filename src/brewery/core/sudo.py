"""Answering sudo's password prompts from brewery, through an askpass helper."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from brewery.core.errors import CellarError
from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

# Run by sudo with its prompt as argv[1]; prints brewery's reply; an empty
# reply exits 1, which sudo reports as no password given
_HELPER = """#!{python} -I
import socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect({sock!r})
s.sendall(sys.argv[1].encode() if len(sys.argv) > 1 else b"")
s.shutdown(socket.SHUT_WR)
reply = b"".join(iter(lambda: s.recv(4096), b""))
if not reply:
    sys.exit(1)
sys.stdout.buffer.write(reply + b"\\n")
"""

# How long an answer is reused for; a sudo asking later is prompted again
_SHARE_FOR = 5.0

if sys.platform == "darwin":
    _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")


def _peer_parent(conn: socket.socket) -> int:
    """The parent of the process on the other end of `conn`: sudo, for the helper.

    Read from the kernel, so the peer can't lie about it.

    Args:
        conn: A connected unix socket.

    Returns:
        The peer's parent pid.
    """
    if sys.platform == "darwin":
        pid = conn.getsockopt(0, 0x002)  # SOL_LOCAL, LOCAL_PEERPID
        info = ctypes.create_string_buffer(136)  # struct proc_bsdinfo

        # PROC_PIDTBSDINFO; allowed for the helper
        if _libproc.proc_pidinfo(pid, 3, 0, info, 136) != 136:
            raise OSError(f"no process info for pid {pid}")

        return struct.unpack_from("I", info, 16)[0]  # pbi_ppid

    pid = struct.unpack(
        "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )[0]

    # Fields after the parenthesised command name: state, then ppid
    return int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[1])


def _is_sudo(pid: int) -> bool:
    """Whether `pid` is sudo, so the password it gets goes to sudo alone.

    macOS reads the executable's path, which proc_pidpath gives even for root's
    processes; Linux hides a setuid process's exe, so it checks the process holds
    root and is named sudo instead.

    Args:
        pid: The process to check.

    Returns:
        True if it is sudo (macOS) or a privileged process named sudo (Linux).
    """
    if sys.platform == "darwin":
        path = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
        _libproc.proc_pidpath(pid, path, 4096)

        # Only the SIP-sealed default macOS sudo
        return path.value == b"/usr/bin/sudo"

    status = dict(
        line.split(":", 1)
        for line in Path(f"/proc/{pid}/status").read_text().splitlines()
    )

    # Effective or saved uid
    return (
        status.get("Name", "").strip() == "sudo"
        and "0" in status.get("Uid", "").split()[1:3]
    )


@contextmanager
def askpass(ask: Callable[[str], str | None]) -> Generator[None]:
    """Point `SUDO_ASKPASS` at a helper that answers sudo's prompts with `ask`.

    `ask` runs on the single-threaded server, so prompts never overlap; sudos
    queued behind a prompt reuse its answer for `_SHARE_FOR` seconds; a sudo
    asking again (wrong answer) is prompted afresh.

    Args:
        ask: Called with sudo's prompt; returns the password, or None to decline.

    Yields:
        None, with `SUDO_ASKPASS` set; the old value is restored and the helper
        removed on exit.
    """

    shared: list[tuple[str | None, float]] = []  # The last answer, and when
    served: set[int] = set()  # The sudos using it

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            prompt = self.rfile.read().decode(errors="replace")
            try:
                pid = _peer_parent(self.connection)
                if not _is_sudo(pid):
                    return  # Declined: only sudo gets the password

            except (OSError, ValueError):
                return

            # A sudo back again was refused the answer; a stale one isn't reused
            if shared and (
                pid in served or time.monotonic() - shared[0][1] > _SHARE_FOR
            ):
                shared.clear()
                served.clear()

            if not shared:
                shared.append((ask(prompt), time.monotonic()))

            served.add(pid)
            reply = shared[0][0]
            if reply:
                with contextlib.suppress(OSError):
                    self.wfile.write(reply.encode())

    # macOS caps socket paths at 104 bytes; $TMPDIR plus a short name fits, move to a
    # shorter base dir if that ever breaks
    with (
        tempfile.TemporaryDirectory(
            prefix="brewery-", ignore_cleanup_errors=True
        ) as tmp,
        socketserver.UnixStreamServer(sock := f"{tmp}/s", Handler) as server,
    ):
        helper = Path(tmp, "askpass")
        helper.write_text(_HELPER.format(python=sys.executable, sock=sock))
        helper.chmod(0o700)

        # Default poll_interval is 0.5s
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()

        old = os.environ.get("SUDO_ASKPASS")
        os.environ["SUDO_ASKPASS"] = str(helper)

        try:
            yield

        finally:
            if old is None:
                os.environ.pop("SUDO_ASKPASS", None)

            else:
                os.environ["SUDO_ASKPASS"] = old

            server.shutdown()


async def validate() -> None:
    """Ask for sudo's password upfront when it's known to be needed.

    sudo's timestamp is keyed on the tty and session, which brew shares; a
    declined or failed ask is only logged: brew asks again when needed.
    """
    if os.environ.get("HOMEBREW_NO_SUDO"):
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo",
            "-A",
            "-v",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        code = await proc.wait()

    except OSError as exc:
        log.warning(event="sudo_validate_failed", error=str(exc))
        return

    if code:
        log.warning(event="sudo_validate_failed", returncode=code)


def run_sudo(
    *args: str,
    stdin: bytes = b"",
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command as root.

    sudo resets the environment, so `env` is set through `/usr/bin/env`; `-A`
    lets `askpass` answer a prompt when it's set up.

    Args:
        *args: The command and its arguments.
        stdin: Bytes to feed the command.
        env: Variables to set for the command, on top of sudo's reset environment.
        check: Raise if the command exits non-zero.

    Returns:
        The completed process.

    Raises:
        CellarError: If `HOMEBREW_NO_SUDO` is set, sudo can't be run, or the
            command fails and `check` is set.
    """
    if os.environ.get("HOMEBREW_NO_SUDO"):
        raise CellarError(f"{args[0]} needs sudo, which HOMEBREW_NO_SUDO disables")

    askpass_flag = ["-A"] if "SUDO_ASKPASS" in os.environ else []
    assigns = [f"{k}={v}" for k, v in (env or {}).items()]
    cmd = ["/usr/bin/sudo", *askpass_flag, "--", "/usr/bin/env", *assigns, *args]
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, check=False)

    except OSError as exc:
        raise CellarError(f"can't run sudo: {exc}") from exc

    if check and proc.returncode:
        out = (proc.stderr or proc.stdout).decode(errors="replace").strip()
        raise CellarError(f"{args[0]} failed: {out}")

    return proc
