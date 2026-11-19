"""Unit tests for the sudo askpass helper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from brewery.core import sudo
from brewery.core.sudo import askpass


def _run_helper(prompt: str) -> subprocess.CompletedProcess[str]:
    """Run the helper the way sudo does, with the prompt as its argument.

    Args:
        prompt: The prompt to pass as the helper's argv[1].

    Returns:
        The finished process, with its stdout and stderr captured as text.
    """
    return subprocess.run(
        [os.environ["SUDO_ASKPASS"], prompt],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _run_from_shell(prompt: str) -> subprocess.CompletedProcess[str]:
    """As `_run_helper`, but from a shell of its own, standing in for a new sudo.

    `; :` stops the shell exec'ing the helper in its place.

    Args:
        prompt: The prompt to pass as the helper's argv[1].

    Returns:
        The finished process, with its stdout and stderr captured as text.
    """
    return subprocess.run(
        ["sh", "-c", '"$SUDO_ASKPASS" "$0"; :', prompt],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestAskpass:
    """Tests the helper's round trip through `ask`, and the cleanup after."""

    @pytest.fixture(autouse=True)
    def _any_parent_is_sudo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Treat the test's processes as sudo, which they aren't."""
        monkeypatch.setattr(sudo, "_is_sudo", lambda _pid: True)

    def test_prompt_reaches_ask_and_answer_is_printed(self) -> None:
        """Test that sudo's prompt reaches `ask` and its answer is printed."""
        prompts: list[str] = []

        def ask(prompt: str) -> str:
            prompts.append(prompt)
            return "hunter2"

        with askpass(ask):
            result = _run_helper("Password:")

        assert prompts == ["Password:"]
        assert result.returncode == 0
        assert result.stdout == "hunter2\n"

    def test_declining_exits_1_with_no_output(self) -> None:
        """Test that `ask` returning None makes the helper exit 1 with no output."""
        with askpass(lambda _prompt: None):
            result = _run_helper("Password:")

        assert result.returncode == 1
        assert result.stdout == ""

    def test_queued_sudos_share_one_prompt(self) -> None:
        """Test that sudos other than the one prompted get its answer unasked."""
        prompts: list[str] = []

        def ask(prompt: str) -> str:
            prompts.append(prompt)
            return "hunter2"

        with askpass(ask):
            results = [_run_from_shell("Password:") for _ in range(2)]

        assert prompts == ["Password:"]
        assert [r.stdout for r in results] == ["hunter2\n", "hunter2\n"]

    def test_same_sudo_asking_again_prompts_afresh(self) -> None:
        """Test that a sudo asking again, having been refused, is prompted anew."""
        answers = iter(["wrong", "right"])

        with askpass(lambda _prompt: next(answers)):
            first, second = _run_helper("Password:"), _run_helper("Password:")

        assert (first.stdout, second.stdout) == ("wrong\n", "right\n")

    def test_stale_answer_is_not_shared(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a sudo asking after the window is prompted, not given the old answer."""
        monkeypatch.setattr(sudo, "_SHARE_FOR", 0.0)
        answers = iter(["first", "second"])

        with askpass(lambda _prompt: next(answers)):
            results = [_run_from_shell("Password:") for _ in range(2)]

        assert [r.stdout for r in results] == ["first\n", "second\n"]

    @pytest.mark.parametrize("before", [None, "/usr/bin/other-askpass"])
    def test_restores_env_and_removes_dir(
        self, before: str | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that `SUDO_ASKPASS` is restored and the helper's directory is gone after exit."""
        if before is None:
            monkeypatch.delenv("SUDO_ASKPASS", raising=False)
        else:
            monkeypatch.setenv("SUDO_ASKPASS", before)

        with askpass(lambda _prompt: None):
            helper = Path(os.environ["SUDO_ASKPASS"])
            assert helper.is_file()

        assert os.environ.get("SUDO_ASKPASS") == before
        assert not helper.parent.exists()


def test_non_sudo_parent_is_declined_unasked() -> None:
    """Test that a helper not run by sudo gets no answer, and `ask` isn't called."""
    prompts: list[str] = []

    with askpass(lambda prompt: prompts.append(prompt) or "hunter2"):
        result = _run_helper("Password:")

    assert prompts == []
    assert result.returncode == 1
    assert result.stdout == ""


def test_is_sudo_refuses_an_unprivileged_process() -> None:
    """Test that `_is_sudo` refuses this test's own, unprivileged process."""
    assert not sudo._is_sudo(os.getpid())


@pytest.mark.parametrize(
    ("name", "uids", "expected"),
    [
        ("sudo", "501\t0\t0\t0", True),  # Real sudo: root effective and saved
        ("sudo", "501\t501\t0\t501", True),  # Dropped to us for now, root saved
        ("systemd", "0\t0\t0\t0", False),  # Init, which adopts orphans
        ("sudo", "501\t501\t501\t501", False),  # One of ours, renamed sudo
    ],
)
def test_is_sudo_on_linux(
    name: str,
    uids: str,
    expected: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that Linux takes only a process named sudo and holding root as sudo."""
    status = tmp_path / "status"
    status.write_text(f"Name:\t{name}\nUmask:\t0022\nUid:\t{uids}\n")
    monkeypatch.setattr(sudo.sys, "platform", "linux")
    monkeypatch.setattr(sudo, "Path", lambda _path: status)

    assert sudo._is_sudo(1) is expected


@pytest.mark.parametrize(("code", "warned"), [(0, False), (1, True)])
async def test_validate_runs_sudo_askpass_and_warns_on_failure(
    monkeypatch, caplog, code: int, warned: bool
) -> None:
    """Test that validate runs `sudo -A -v` and only warns on a non-zero exit."""
    import logging

    argv: list[tuple[str, ...]] = []

    class Proc:
        async def wait(self) -> int:
            return code

    async def fake_exec(*args: str, **_: object) -> Proc:
        argv.append(args)
        return Proc()

    monkeypatch.setattr(sudo.asyncio, "create_subprocess_exec", fake_exec)

    with caplog.at_level(logging.WARNING, logger="brewery.core.sudo"):
        await sudo.validate()

    assert argv == [("sudo", "-A", "-v")]
    assert (
        any("sudo_validate_failed" in r.getMessage() for r in caplog.records) is warned
    )
