"""Unit tests for run_brew_command's result/error mapping over a mockd run_capture."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

import pytest

from brewery.core import shell
from brewery.core.errors import BrewCommandError, BrewTimeoutError
from brewery.core.shell import BrewOutput, run_brew

pytestmark = pytest.mark.asyncio


class MockProc:
    """Mock process for testing.

    Models a live child: `returncode` stays None until the process exits, which
    is what `run_brew`'s teardown checks before it signals anything.
    """

    def __init__(
        self, returncode=0, stdout=b"", stderr=b"", hang=False, ignore_sigint=False
    ) -> None:
        """Initialises the mock process.

        Args:
            returncode: The exit code the process reports once it exits.
            stdout: The standard output of the process.
            stderr: The standard error of the process.
            hang: Whether the process should run until it is signalled.
            ignore_sigint: Whether the process survives SIGINT, as a build that
                will only go down on SIGKILL does.
        """
        self.pid = 424242
        self.returncode = None if hang else returncode
        self.ignore_sigint = ignore_sigint
        self.signals: list[int] = []
        self._exit_code = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self._exited = asyncio.Event()
        if not hang:
            self._exited.set()

    async def communicate(self) -> tuple[bytes, bytes]:
        """Communicates with the mock process and returns its output.

        Returns:
            The standard output and error of the process.
        """
        if self._hang:
            await asyncio.sleep(10)

        return self._stdout, self._stderr

    async def wait(self) -> int:
        """Waits for the mock process to exit and returns its exit code.

        Returns:
            The exit code of the process.
        """
        await self._exited.wait()

        return self._exit_code

    def deliver(self, sig: int) -> None:
        """Record a signal, and exit unless the process ignores it.

        Args:
            sig: The signal delivered to the process.
        """
        self.signals.append(sig)
        if sig == signal.SIGKILL or not self.ignore_sigint:
            self.returncode = self._exit_code
            self._exited.set()

    def send_signal(self, sig: int) -> None:
        """Signal the process directly, as an undetached child is signalled.

        Args:
            sig: The signal to send.
        """
        self.deliver(sig)


def _patch_group_signals(monkeypatch, proc) -> list[int]:
    """Route the module's process-group signalling into `proc`.

    A captured run is detached into its own process group and signalled by
    group, so the real `os` calls would otherwise target a pid that is not ours.

    Args:
        monkeypatch: The monkeypatch fixture.
        proc: The mock process the group resolves to.

    Returns:
        The list each group signal is recorded in.
    """
    sent: list[int] = []

    monkeypatch.setattr(shell.os, "getpgid", lambda pid: pid)

    def mock_killpg(pgid, sig) -> None:
        """Deliver a group signal to the mock process.

        Args:
            pgid: The process group id, ignored.
            sig: The signal to deliver.
        """
        sent.append(sig)
        proc.deliver(sig)

    monkeypatch.setattr(shell.os, "killpg", mock_killpg)

    return sent


def _patch(monkeypatch, proc, *, have_brew=True) -> dict[str, Any]:
    """Patches the environment for testing.

    Args:
        monkeypatch: The monkeypatch fixture.
        proc: The mock process to return.
        have_brew: Whether the brew command is available. When False the exec raises
            FileNotFoundError, which is how a missing brew presents itself.

    Returns:
        A dict accumulating the `cmd`, `stdout`, and `stderr` args
        passed to the most recent `create_subprocess_exec` call.
    """
    calls = {}

    async def mock_exec(*cmd, stdout=None, stderr=None, env=None, **kwargs) -> MockProc:
        """Mocks the execution of a subprocess.

        Args:
            cmd: The command to execute.
            stdout: The standard output stream.
            stderr: The standard error stream.
            env: The environment mapping passed to the child.
            **kwargs: Other create_subprocess_exec args (e.g. stdin,
                start_new_session) captured for assertions.

        Returns:
            A mock process with the specified output.

        Raises:
            FileNotFoundError: `have_brew` is False.
        """
        if not have_brew:
            raise FileNotFoundError(2, "No such file or directory", "brew")

        calls["cmd"] = cmd
        calls["stdout"] = stdout
        calls["stderr"] = stderr
        calls["env"] = env
        calls["stdin"] = kwargs.get("stdin")
        calls["start_new_session"] = kwargs.get("start_new_session")

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

    # CAPTURE forces a stable locale / no colour so output parsing is robust
    assert calls["env"]["LANG"] == "C"
    assert calls["env"]["HOMEBREW_NO_COLOR"] == "1"

    # CAPTURE detaches from the controlling terminal (new session, no stdin) so a
    # child cannot write to /dev/tty and corrupt a live progress display
    assert calls["start_new_session"] is True
    assert calls["stdin"] == asyncio.subprocess.DEVNULL


async def test_inherit_does_not_pipe(monkeypatch) -> None:
    """Test that INHERIT mode leaves stdio as None so the child inherits the terminal."""
    calls = _patch(monkeypatch, MockProc(0))
    res = await run_brew(["install", "wget"], output=BrewOutput.INHERIT)

    # INHERIT leaves stdio as None so the child inherits the terminal
    assert calls["stdout"] is None and calls["stderr"] is None
    assert res.stdout == "" and res.stderr == "" and res.returncode == 0

    # INHERIT keeps the user's environment (colour/locale) untouched
    assert calls["env"] is None

    # INHERIT stays attached to the terminal (interactive passthrough)
    assert calls["start_new_session"] is False and calls["stdin"] is None


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


async def test_nonzero_is_logged_as_a_failure_under_no_check(
    monkeypatch, caplog
) -> None:
    """Test that a non-zero exit is recorded as a failure even when not raised.

    check=False callers decide for themselves what a non-zero exit means, so
    this line is the only record of what brew actually said.
    """
    _patch(monkeypatch, MockProc(1, b"", b"Error: uv: no bottle available!"))
    with caplog.at_level(logging.WARNING, logger="brewery.core.shell"):
        await run_brew(["upgrade", "uv"], check=False)

    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert "brew_command_failed" in record.getMessage()
    assert "no bottle available" in record.getMessage()
    assert "brew_command_success" not in caplog.text


async def test_failure_under_check_is_logged_at_error(monkeypatch, caplog) -> None:
    """Test that a non-zero exit the caller wanted raised is logged at ERROR."""
    _patch(monkeypatch, MockProc(1, b"", b"boom"))
    with (
        caplog.at_level(logging.WARNING, logger="brewery.core.shell"),
        pytest.raises(BrewCommandError),
    ):
        await run_brew(["install", "nope"], check=True)

    assert caplog.records[-1].levelno == logging.ERROR


async def test_multiline_brew_output_is_logged_on_one_line(monkeypatch, caplog) -> None:
    """Test that brew's multi-line failures stay one greppable log line."""
    _patch(monkeypatch, MockProc(1, b"", b"Error: no bottle\nTry:\n  brew install"))
    with caplog.at_level(logging.WARNING, logger="brewery.core.shell"):
        res = await run_brew(["upgrade", "uv"], check=False)

    assert "\n" not in caplog.records[-1].getMessage()

    # The caller still gets brew's output intact
    assert res.stderr == "Error: no bottle\nTry:\n  brew install"


async def test_long_brew_output_is_truncated_in_the_log(monkeypatch, caplog) -> None:
    """Test that a build's output cannot flood the log, but reaches the caller."""
    noise = "x" * 5000
    _patch(monkeypatch, MockProc(1, b"", noise.encode()))
    with caplog.at_level(logging.WARNING, logger="brewery.core.shell"):
        res = await run_brew(["upgrade", "grpc"], check=False)

    assert len(caplog.records[-1].getMessage()) < 1000
    assert res.stderr == noise


async def test_success_is_logged_as_success(monkeypatch, caplog) -> None:
    """Test that a zero exit is still recorded as a success."""
    _patch(monkeypatch, MockProc(0, b"ok", b""))
    with caplog.at_level(logging.INFO, logger="brewery.core.shell"):
        await run_brew(["info", "wget"])

    assert "brew_command_success" in caplog.text
    assert "brew_command_failed" not in caplog.text


async def test_timeout_kills_and_raises(monkeypatch) -> None:
    """Test that exceeding the timeout stops the process and raises BrewTimeoutError."""
    proc = MockProc(hang=True)
    _patch(monkeypatch, proc)
    sent = _patch_group_signals(monkeypatch, proc)

    with pytest.raises(BrewTimeoutError):
        await run_brew(["install", "slow"], timeout=0.01)

    assert sent == [signal.SIGINT]
    assert proc.returncode is not None


async def test_cancel_stops_the_detached_child(monkeypatch) -> None:
    """Test that cancelling a captured run takes its brew process down with it.

    A captured child is detached from the terminal, so a Ctrl-C never reaches
    it: cancellation is the only thing standing between an abandoned brew and a
    source build that outlives brewery.
    """
    proc = MockProc(hang=True)
    _patch(monkeypatch, proc)
    sent = _patch_group_signals(monkeypatch, proc)

    task = asyncio.create_task(run_brew(["upgrade", "grpc"]))
    await asyncio.sleep(0)  # Let the task reach its await on the process
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sent == [signal.SIGINT]
    assert proc.returncode is not None


async def test_child_that_ignores_sigint_is_killed(monkeypatch) -> None:
    """Test that a process which will not take SIGINT is escalated to SIGKILL."""
    monkeypatch.setattr(shell, "_GRACE", 0.01)
    proc = MockProc(hang=True, ignore_sigint=True)
    _patch(monkeypatch, proc)
    sent = _patch_group_signals(monkeypatch, proc)

    task = asyncio.create_task(run_brew(["upgrade", "grpc"]))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sent == [signal.SIGINT, signal.SIGKILL]


async def test_inherit_signals_the_child_directly(monkeypatch) -> None:
    """Test that an undetached child is signalled directly, not by group.

    INHERIT mode leaves the child in brewery's own process group; signalling
    that group would take brewery down with it.
    """
    proc = MockProc(hang=True)
    _patch(monkeypatch, proc)
    group = _patch_group_signals(monkeypatch, proc)

    task = asyncio.create_task(run_brew(["install", "wget"], output=BrewOutput.INHERIT))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert group == []
    assert proc.signals == [signal.SIGINT]


async def test_missing_brew_raises(monkeypatch) -> None:
    """Test that a missing brew binary raises BrewCommandError with returncode 127."""
    _patch(monkeypatch, MockProc(0), have_brew=False)
    with pytest.raises(BrewCommandError) as ei:
        await run_brew(["install", "wget"])
    assert ei.value.context["returncode"] == 127
