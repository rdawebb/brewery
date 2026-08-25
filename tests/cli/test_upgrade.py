"""Tests the upgrade command's three-way prompt and its result reporting."""

from __future__ import annotations

import pytest

from brewery.cli.commands import upgrade as upgrade_mod
from brewery.core.errors import EXIT_USER_ERROR, PinnedPackageWarning
from brewery.core.models import Package, PackageKind


def _pkg(name: str, version: str = "1.0", latest: str | None = None) -> Package:
    """An installed Package, optionally carrying the latest version metadata.

    Args:
        name: The package name.
        version: The installed version.
        latest: The upstream version, as `get_outdated` reports it.

    Returns:
        A Package the upgrade command's renderers can format.
    """
    return Package(
        name=name,
        kind=PackageKind.FORMULA,
        versions=[version],
        metadata={"latest_version": latest} if latest else {},
    )


@pytest.fixture
def upgrades(monkeypatch):
    """Stub upgrade_packages and record the arguments it was handed.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        A callable configuring the four-tuple result and returning the call log.
    """

    def _arrange(upgraded=(), current=(), advisories=(), failures=(), raises=None):
        calls: list[dict] = []

        async def _upgrade(repo, names, kind, *, progress=None):
            calls.append({"names": names, "kind": kind, "progress": progress})
            if raises is not None:
                raise raises

            return list(upgraded), list(current), list(advisories), list(failures)

        monkeypatch.setattr(upgrade_mod, "upgrade_packages", _upgrade)

        return calls

    return _arrange


@pytest.fixture
def outdated(monkeypatch):
    """Make `repo.get_outdated()` report a fixed list.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        A callable taking the packages `get_outdated` should report.
    """

    def _arrange(pkgs, repo):
        monkeypatch.setattr(type(repo), "get_outdated", lambda self: list(pkgs))

    return _arrange


class TestPromptBranches:
    """Tests the three ways upgrade decides whether to ask before acting."""

    def test_the_yes_flag_skips_every_prompt(self, run, upgrades) -> None:
        """Test that --yes upgrades without reading stdin or listing first."""
        calls = upgrades(upgraded=[_pkg("wget", "1.25.0")])

        result = run(["upgrade", "--yes"])

        assert result.exit_code == 0
        assert calls[0]["names"] is None

    def test_named_packages_are_confirmed_by_name(self, run, upgrades) -> None:
        """Test that naming packages prompts with those names and nothing else."""
        calls = upgrades(upgraded=[_pkg("wget")])

        result = run(["upgrade", "wget"], input="y\n")

        assert result.exit_code == 0
        assert "Upgrade: wget?" in result.output
        assert calls[0]["names"] == ["wget"]

    def test_declining_a_named_upgrade_does_nothing(self, run, upgrades) -> None:
        """Test that answering no to a named upgrade never reaches the service."""
        calls = upgrades()

        result = run(["upgrade", "wget"], input="n\n")

        assert result.exit_code == 0
        assert calls == []

    def test_a_bare_upgrade_lists_what_is_outdated_first(
        self, run, upgrades, outdated, repo
    ) -> None:
        """Test that upgrading everything shows the list before asking."""
        outdated([_pkg("wget", "1.24.5", latest="1.25.0")], repo)
        calls = upgrades(upgraded=[_pkg("wget", "1.25.0")])

        result = run(["upgrade"], input="y\n")

        assert "1 outdated package(s)" in result.output
        assert "wget → 1.25.0" in result.output
        assert calls[0]["names"] is None

    def test_declining_the_bulk_upgrade_does_nothing(
        self, run, upgrades, outdated, repo
    ) -> None:
        """Test that answering no to the bulk prompt never reaches the service."""
        outdated([_pkg("wget", "1.24.5", latest="1.25.0")], repo)
        calls = upgrades()

        result = run(["upgrade"], input="n\n")

        assert result.exit_code == 0
        assert calls == []

    def test_nothing_outdated_short_circuits_before_prompting(
        self, run, upgrades, outdated, repo
    ) -> None:
        """Test that an up-to-date machine reports so instead of asking."""
        outdated([], repo)
        calls = upgrades()

        result = run(["upgrade"])

        assert result.exit_code == 0
        assert "All packages are up to date" in result.output
        assert calls == []


class TestReporting:
    """Tests what upgrade prints once the service has returned."""

    def test_an_empty_result_reports_nothing_to_do(self, run, upgrades) -> None:
        """Test the guard for a service that upgraded nothing and failed nothing."""
        upgrades()

        result = run(["upgrade", "--yes"])

        assert result.exit_code == 0
        assert "All packages are up to date" in result.output
        assert "Upgraded 0" not in result.output

    def test_upgraded_packages_are_listed(self, run, upgrades) -> None:
        """Test that each upgraded package appears with its new version."""
        upgrades(upgraded=[_pkg("wget", "1.25.0"), _pkg("curl", "8.0")])

        result = run(["upgrade", "--yes"])

        assert "Upgraded 2 package(s)" in result.output
        assert "wget 1.25.0" in result.output
        assert "curl 8.0" in result.output

    def test_already_current_packages_are_listed_separately(
        self, run, upgrades
    ) -> None:
        """Test that packages needing no work are reported, not silently dropped."""
        upgrades(upgraded=[_pkg("wget", "1.25.0")], current=[_pkg("curl", "8.0")])

        result = run(["upgrade", "--yes"])

        assert "1 already up-to-date" in result.output
        assert "curl 8.0" in result.output

    def test_advisories_are_shown_without_failing(self, run, upgrades) -> None:
        """Test that a pinned package warns but does not change the exit code."""
        upgrades(upgraded=[_pkg("wget")], advisories=[("curl", "pinned")])

        result = run(["upgrade", "--yes"])

        assert result.exit_code == 0
        assert "curl - pinned" in result.output

    def test_a_failure_exits_user_error(self, run, upgrades) -> None:
        """Test that a failed upgrade reports and exits non-zero."""
        upgrades(upgraded=[_pkg("wget")], failures=[("curl", "checksum mismatch")])

        result = run(["upgrade", "--yes"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "curl - checksum mismatch" in result.output

    def test_a_pinned_warning_is_advisory(self, run, upgrades) -> None:
        """Test that PinnedPackageWarning prints and exits 0.

        `@command_error(warnings=(PinnedPackageWarning,))` makes upgrading a
        pinned formula a warning rather than a failure.
        """
        upgrades(raises=PinnedPackageWarning(package="wget"))

        result = run(["upgrade", "wget", "--yes"])

        assert result.exit_code == 0
        assert "pinned" in result.output

    @pytest.mark.parametrize("alias", ["u", "up"])
    def test_the_aliases_reach_the_same_command(self, run, upgrades, alias) -> None:
        """Test that `brewery u` and `brewery up` are `brewery upgrade`."""
        calls = upgrades(upgraded=[_pkg("wget")])

        run([alias, "wget", "--yes"])

        assert calls[0]["names"] == ["wget"]
