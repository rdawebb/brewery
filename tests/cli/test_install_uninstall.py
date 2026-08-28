"""Tests the install and uninstall commands end to end through CliRunner."""

from __future__ import annotations

import pytest

from brewery.cli.commands import install as install_mod
from brewery.cli.commands import uninstall as uninstall_mod
from brewery.core.errors import EXIT_USER_ERROR, AlreadyInstalledWarning
from brewery.core.models import Package, PackageKind


def _pkg(name: str, version: str = "1.0") -> Package:
    """A minimal installed Package for the result renderers.

    Args:
        name: The package name.
        version: The installed version.

    Returns:
        A Package the output helpers can format.
    """
    return Package(name=name, kind=PackageKind.FORMULA, versions=[version])


@pytest.fixture
def installs(monkeypatch):
    """Stub install_packages and record the arguments it was handed.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        A callable taking the (installed, failures) result to report, and
        returning the list that accumulates one call record per invocation.
    """

    def _arrange(installed=(), failures=(), raises=None):
        calls: list[dict] = []

        async def _install(repo, names, kind, *, progress=None):
            calls.append({"names": names, "kind": kind, "progress": progress})
            if raises is not None:
                raise raises

            return list(installed), list(failures)

        monkeypatch.setattr(install_mod, "install_packages", _install)

        return calls

    return _arrange


@pytest.fixture
def uninstalls(monkeypatch):
    """Stub uninstall_packages and record the arguments it was handed.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        A callable taking the (removed, failures) result to report, and
        returning the list that accumulates one call record per invocation.
    """

    def _arrange(removed=(), failures=()):
        calls: list[dict] = []

        async def _uninstall(repo, names, kind):
            calls.append({"names": names, "kind": kind})

            return list(removed), list(failures)

        monkeypatch.setattr(uninstall_mod, "uninstall_packages", _uninstall)

        return calls

    return _arrange


class TestInstall:
    """Tests the install command's confirmation, reporting, and exit code."""

    def test_a_clean_install_exits_zero_and_lists_what_landed(
        self, run, installs
    ) -> None:
        """Test the happy path: names forwarded, results printed, exit 0."""
        calls = installs(installed=[_pkg("wget", "1.25.0")])

        result = run(["install", "wget", "--yes"])

        assert result.exit_code == 0
        assert calls[0]["names"] == ["wget"]
        assert "Installed 1 package(s)" in result.output
        assert "wget 1.25.0" in result.output

    def test_a_failure_exits_user_error_and_names_the_reason(
        self, run, installs
    ) -> None:
        """Test brew's ofail semantics: report the failure, then exit non-zero."""
        installs(installed=[_pkg("curl")], failures=[("wget", "404 not found")])

        result = run(["install", "wget", "curl", "--yes"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "Failed to install 1 package(s)" in result.output
        assert "wget - 404 not found" in result.output

    def test_declining_the_prompt_installs_nothing(self, run, installs) -> None:
        """Test that answering no exits 0 without reaching the service."""
        calls = installs()

        result = run(["install", "wget"], input="n\n")

        assert result.exit_code == 0
        assert calls == []
        assert "Cancelled" in result.output

    def test_accepting_the_prompt_proceeds(self, run, installs) -> None:
        """Test that answering yes reaches the service."""
        calls = installs(installed=[_pkg("wget")])

        result = run(["install", "wget"], input="y\n")

        assert result.exit_code == 0
        assert calls[0]["names"] == ["wget"]

    def test_the_prompt_defaults_to_proceeding(self, run, installs) -> None:
        """Test that a bare Enter installs, since install is not destructive."""
        calls = installs(installed=[_pkg("wget")])

        result = run(["install", "wget"], input="\n")

        assert result.exit_code == 0
        assert calls[0]["names"] == ["wget"]

    def test_the_kind_defaults_to_formula(self, run, installs) -> None:
        """Test that an unqualified install targets formulae, as brew does."""
        calls = installs(installed=[_pkg("wget")])

        run(["install", "wget", "--yes"])

        assert calls[0]["kind"] is PackageKind.FORMULA

    def test_an_explicit_cask_kind_is_forwarded(self, run, installs) -> None:
        """Test that --kind cask reaches the service unchanged."""
        calls = installs(installed=[_pkg("iina")])

        run(["install", "iina", "--kind", "cask", "--yes"])

        assert calls[0]["kind"] is PackageKind.CASK

    def test_progress_is_suppressed_when_output_is_captured(
        self, run, installs
    ) -> None:
        """Test that a non-tty run passes no reporter into the pipeline.

        Live-display control codes would corrupt piped output, so `make_reporter`
        returns None and the command has to hand that through rather than
        constructing its own.
        """
        calls = installs(installed=[_pkg("wget")])

        run(["install", "wget", "--yes"])

        assert calls[0]["progress"] is None

    def test_an_already_installed_warning_is_advisory(self, run, installs) -> None:
        """Test that re-installing exits 0 with a warning, not an error.

        `@command_error(warnings=(AlreadyInstalledWarning,))` makes this the one
        exception that prints and succeeds.
        """
        installs(raises=AlreadyInstalledWarning(package="wget"))

        result = run(["install", "wget", "--yes"])

        assert result.exit_code == 0
        assert "already installed" in result.output

    def test_the_add_alias_reaches_the_same_command(self, run, installs) -> None:
        """Test that `brewery add` is `brewery install`."""
        calls = installs(installed=[_pkg("wget")])

        run(["add", "wget", "--yes"])

        assert calls[0]["names"] == ["wget"]


class TestUninstall:
    """Tests the uninstall command, whose prompt default is load-bearing."""

    def test_the_prompt_defaults_to_cancelling(self, run, uninstalls) -> None:
        """Test that a bare Enter cancels rather than deleting.

        uninstall is the only caller passing default=False to confirm_or_cancel,
        to prevent accidental uninstalls.
        """
        calls = uninstalls()

        result = run(["uninstall", "wget"], input="\n")

        assert result.exit_code == 0
        assert calls == [], "a bare Enter reached the uninstall service"
        assert "Cancelled" in result.output

    def test_a_closed_stdin_cancels(self, run, uninstalls) -> None:
        """Test that a non-interactive uninstall does not delete anything.

        `confirm_or_cancel`'s EOFError branch is what makes piping into
        `brewery uninstall` safe.
        """
        calls = uninstalls()

        result = run(["uninstall", "wget"], input="")

        assert result.exit_code == 0
        assert calls == []

    def test_an_explicit_yes_proceeds(self, run, uninstalls) -> None:
        """Test that answering yes does reach the service."""
        calls = uninstalls(removed=["wget"])

        result = run(["uninstall", "wget"], input="y\n")

        assert result.exit_code == 0
        assert calls[0]["names"] == ["wget"]
        assert "Uninstalled 1 package(s)" in result.output

    def test_the_yes_flag_skips_the_prompt(self, run, uninstalls) -> None:
        """Test that --yes needs no stdin at all."""
        calls = uninstalls(removed=["wget"])

        result = run(["uninstall", "wget", "--yes"])

        assert result.exit_code == 0
        assert calls[0]["names"] == ["wget"]

    def test_a_failure_exits_user_error(self, run, uninstalls) -> None:
        """Test that a failed removal reports and exits non-zero."""
        uninstalls(removed=[], failures=[("wget", "still linked")])

        result = run(["uninstall", "wget", "--yes"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "wget - still linked" in result.output

    def test_the_kind_is_left_unset_by_default(self, run, uninstalls) -> None:
        """Test that uninstall auto-detects rather than assuming formula."""
        calls = uninstalls(removed=["iina"])

        run(["uninstall", "iina", "--yes"])

        assert calls[0]["kind"] is None

    @pytest.mark.parametrize("alias", ["rm", "del"])
    def test_the_aliases_reach_the_same_command(self, run, uninstalls, alias) -> None:
        """Test that `brewery rm` and `brewery del` are `brewery uninstall`."""
        calls = uninstalls(removed=["wget"])

        run([alias, "wget", "--yes"])

        assert calls[0]["names"] == ["wget"]
