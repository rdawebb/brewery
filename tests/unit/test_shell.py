"""Unit tests for run_brew_command's result/error mapping over a mockd run_capture."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import brewery.core.shell as shell
from brewery.core.errors import BrewCommandError, BrewTimeoutError
from brewery.core.shell import BrewOutput, run_brew

pytestmark = pytest.mark.asyncio


class MockProc:
    """Mock process for testing."""

    def __init__(self, returncode=0, stdout=b"", stderr=b"", hang=False) -> None:
        """Initialises the mock process.

        Args:
            returncode: The return code of the process.
            stdout: The standard output of the process.
            stderr: The standard error of the process.
            hang: Whether the process should hang indefinitely.
        """
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        """Communicates with the mock process and returns its output.

        Returns:
            The standard output and error of the process.
        """
        if self._hang:
            await asyncio.sleep(10)

        return self._stdout, self._stderr

    async def wait(self) -> int:
        """Waits for the mock process to complete and returns its exit code.

        Returns:
            The exit code of the process.
        """
        if self._hang and not self.killed:
            await asyncio.sleep(10)

        return self.returncode

    def kill(self) -> None:
        """Kills the mock process."""
        self.killed = True


def _patch(monkeypatch, proc, *, have_brew=True) -> dict[str, Any]:
    """Patches the environment for testing.

    Args:
        monkeypatch: The monkeypatch fixture.
        proc: The mock process to return.
        have_brew: Whether the brew command is available.

    Returns:
        A dict accumulating the `cmd`, `stdout`, and `stderr` args
        passed to the most recent `create_subprocess_exec` call.
    """
    monkeypatch.setattr(
        shell.shutil, "which", lambda _: "/usr/bin/brew" if have_brew else None
    )
    calls = {}

    async def mock_exec(*cmd, stdout=None, stderr=None) -> MockProc:
        """Mocks the execution of a subprocess.

        Args:
            cmd: The command to execute.
            stdout: The standard output stream.
            stderr: The standard error stream.

        Returns:
            A mock process with the specified output.
        """
        calls["cmd"] = cmd
        calls["stdout"] = stdout
        calls["stderr"] = stderr

        return proc

    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", mock_exec)

    return calls


async def test_capture_returns_decoded_output(monkeypatch) -> None:
    """Test that CAPTURE mode decodes stdout/stderr and pipes both streams."""
    calls = _patch(monkeypatch, MockProc(0, b"hello out", b"warn err"))
    res = await run_brew(["info", "wget"], output=BrewOutput.CAPTURE)
    assert (res.stdout, res.stderr, res.returncode) == ("hello out", "warn err", 0)

    # CAPTURE pipes both streams
    assert calls["stdout"] is not None and calls["stderr"] is not None
    assert calls["cmd"][0] == "brew"


async def test_inherit_does_not_pipe(monkeypatch) -> None:
    """Test that INHERIT mode leaves stdio as None so the child inherits the terminal."""
    calls = _patch(monkeypatch, MockProc(0))
    res = await run_brew(["install", "wget"], output=BrewOutput.INHERIT)

    # INHERIT leaves stdio as None so the child inherits the terminal
    assert calls["stdout"] is None and calls["stderr"] is None
    assert res.stdout == "" and res.stderr == "" and res.returncode == 0


async def test_check_raises_on_nonzero(monkeypatch) -> None:
    """Test that check=True raises BrewCommandError on a non-zero exit code."""
    _patch(monkeypatch, MockProc(1, b"", b"boom"))
    with pytest.raises(BrewCommandError) as ei:
        await run_brew(["install", "nope"], check=True)
    assert ei.value.context["returncode"] == 1


async def test_no_check_returns_nonzero(monkeypatch) -> None:
    """Test that check=False returns the result even on a non-zero exit code."""
    _patch(monkeypatch, MockProc(1, b"out", b"err"))
    res = await run_brew(["install", "nope"], check=False)
    assert res.returncode == 1 and res.stderr == "err"


async def test_timeout_kills_and_raises(monkeypatch) -> None:
    """Test that exceeding the timeout kills the process and raises BrewTimeoutError."""
    proc = MockProc(hang=True)
    _patch(monkeypatch, proc)
    with pytest.raises(BrewTimeoutError):
        await run_brew(["install", "slow"], timeout=0.01)
    assert proc.killed


async def test_missing_brew_raises(monkeypatch) -> None:
    """Test that a missing brew binary raises BrewCommandError with returncode 127."""
    _patch(monkeypatch, MockProc(0), have_brew=False)
    with pytest.raises(BrewCommandError) as ei:
        await run_brew(["install", "wget"])
    assert ei.value.context["returncode"] == 127
