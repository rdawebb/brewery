"""Unit tests for the sudo askpass helper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

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


class TestAskpass:
    """Tests the helper's round trip through `ask`, and the cleanup after."""

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
