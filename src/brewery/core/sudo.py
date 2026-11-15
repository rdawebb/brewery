"""Answering sudo's password prompts from brewery, through an askpass helper."""

from __future__ import annotations

import contextlib
import os
import socketserver
import sys
import tempfile
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

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


@contextmanager
def askpass(ask: Callable[[str], str | None]) -> Generator[None]:
    """Point `SUDO_ASKPASS` at a helper that answers sudo's prompts with `ask`.

    `ask` takes sudo's prompt and returns the password, or None to decline; it
    runs on the server's thread; the server is not threaded, so prompts are
    answered one at a time and never overlap.

    Args:
        ask: Called with sudo's prompt; returns the password, or None to decline.

    Yields:
        None, with `SUDO_ASKPASS` set; the old value is restored and the helper
        removed on exit.
    """

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            reply = ask(self.rfile.read().decode(errors="replace"))
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
