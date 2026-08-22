"""Shared fixtures for Brewery unit tests."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from brewery.core.host import Platform
from brewery.providers.relocator import keg as keg_mod
from brewery.providers.relocator import macho as macho_mod
from brewery.providers.relocator import substitutions as subs_mod
from brewery.providers.relocator import tools as tools_mod


@pytest.fixture
def make_keg() -> Callable[..., Path]:
    """Return a factory that creates a keg dir at `cellar/name/version`.

    The factory signature is `make_keg(cellar, name, version="1.0", *,
    executables=())`. With no executables it just creates the (empty) version
    directory; otherwise it populates `bin/<exe>` with a trivial shell script
    for each name given.

    Returns:
        A callable producing the created keg version directory.
    """

    def _make(
        cellar: Path,
        name: str,
        version: str = "1.0",
        *,
        executables: Sequence[str] = (),
    ) -> Path:
        keg = cellar / name / version
        if executables:
            (keg / "bin").mkdir(parents=True)
            for exe in executables:
                (keg / "bin" / exe).write_text("#!/bin/sh\n")
        else:
            keg.mkdir(parents=True)

        return keg

    return _make


@pytest.fixture
def brew_paths() -> dict:
    """Standard Homebrew prefix/cellar/repository paths used across relocation tests.

    Returns:
        A dict with `prefix`, `cellar`, and `repository` Path values.
    """
    return {
        "prefix": Path("/opt/homebrew"),
        "cellar": Path("/opt/homebrew/Cellar"),
        "repository": Path("/opt/homebrew/Library/Homebrew"),
    }


@pytest.fixture
def force_install_name_tool(monkeypatch) -> None:
    """Disable the in-process rewriter, as `BREWERY_NO_NATIVE_MACHO=1` does.

    Args:
        monkeypatch: The monkeypatch fixture.
    """
    monkeypatch.setattr(macho_mod, "_NATIVE_MACHO", False)


@pytest.fixture
def subs(brew_paths) -> dict[bytes, bytes]:
    """Fixture for building substitution mappings.

    Returns:
        A dictionary mapping placeholder bytes to their resolved values.
    """
    return subs_mod.build_substitutions(**brew_paths)


@pytest.fixture
def system_perl(monkeypatch):
    """Pretend only /usr/bin/perl5.34 is present, on a macOS host.

    Keeps the perl-path resolution off the real filesystem (which varies by
    macOS version) and pins the host platform so the token tests behave
    identically on macOS and Linux CI runners.

    Args:
        monkeypatch: The monkeypatch fixture.
    """
    monkeypatch.setattr(
        keg_mod.Path,
        "exists",
        lambda self: str(self) == "/usr/bin/perl5.34",
        raising=False,
    )
    monkeypatch.setattr(
        subs_mod,
        "current_platform",
        lambda: Platform(arch="arm64", os="macos", macos_major=14),
    )


@pytest.fixture
def mock_run(monkeypatch):
    """Patch the relocator's subprocess boundary with a recording stub.

    Call with no args for a success stub, or pass stderr/returncode to simulate
    a tool failure. The returned list records each argv as it is run.

    Args:
        monkeypatch: The monkeypatch fixture.

    Returns:
        A factory that installs the stub and returns the call-log list.
    """

    def install(
        stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> list[list[str]]:
        """Install the stub and return the call-log list.

        Args:
            stdout: stdout text the stub returns in CompletedProcess.
            stderr: stderr text the stub returns in CompletedProcess.
            returncode: The return code the stub reports.

        Returns:
            A list that accumulates one argv list per subprocess.run call.
        """
        runs: list[list[str]] = []

        def stub(cmd, *args, **kwargs) -> subprocess.CompletedProcess:
            """Record the command and return a CompletedProcess stub.

            Args:
                cmd: The command to record and return.
                *args: Additional args to pass to subprocess.run.
                **kwargs: Additional kwargs to pass to subprocess.run.

            Returns:
                A CompletedProcess stub with the given return code and stdout/stderr.
            """
            runs.append(list(cmd))

            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        monkeypatch.setattr(tools_mod.subprocess, "run", stub)

        return runs

    return install
