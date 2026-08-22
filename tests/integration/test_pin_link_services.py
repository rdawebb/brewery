"""Integration tests for the pin and link services over a real prefix."""

from __future__ import annotations

from brewery.core.models import PackageStatus
from brewery.services.link import link_packages, unlink_packages
from brewery.services.pin import pin_packages, unpin_packages


class TestPinAndUnpin:
    """Tests for the pin service."""

    def test_pin_writes_a_record_and_shows_as_pinned(self, repo, mock_env) -> None:
        """Test that a pinned formula reads back as PINNED through the merge."""
        pinned, advisories, failures = pin_packages(repo, ["act"])

        assert (pinned, advisories, failures) == (["act"], [], [])
        assert (mock_env.prefix / "var" / "homebrew" / "pinned" / "act").is_symlink()

        pkg = next(p for p in repo.get_all_installed() if p.name == "act")
        assert PackageStatus.PINNED in pkg.status

    def test_pinning_twice_is_an_advisory_not_a_failure(self, repo) -> None:
        """Test that re-pinning warns and exits clean, as brew's `opoo` path does."""
        pin_packages(repo, ["act"])

        pinned, advisories, failures = pin_packages(repo, ["act"])
        assert (pinned, advisories, failures) == ([], [("act", "already pinned")], [])

    def test_unpinning_an_unpinned_formula_is_an_advisory(self, repo) -> None:
        """Test that unpinning what was never pinned warns rather than failing."""
        unpinned, advisories, failures = unpin_packages(repo, ["act"])

        assert (unpinned, advisories, failures) == ([], [("act", "not pinned")], [])

    def test_pin_of_a_missing_formula_is_a_failure(self, repo) -> None:
        """Test that a name that is not installed is a hard failure, as brew's `ofail` is."""
        pinned, advisories, failures = pin_packages(repo, ["ripgrep"])

        assert (pinned, advisories, failures) == (
            [],
            [],
            [("ripgrep", "not installed")],
        )

    def test_pin_does_not_reach_for_casks(self, repo) -> None:
        """Test that cask tokens are not resolvable as formulae, so they report not installed."""
        _, _, failures = pin_packages(repo, ["iina"])

        assert failures == [("iina", "not installed")]


class TestLinkAndUnlink:
    """Tests for the link service."""

    def test_link_then_unlink_round_trips(self, repo, mock_env) -> None:
        """Test that linking creates the bookkeeping record; unlinking removes it."""
        keg = mock_env.cellar / "act" / "0.2.88"
        (keg / "bin").mkdir(parents=True)
        (keg / "bin" / "act").write_text("#!/bin/sh\n")

        linked, _, failures = link_packages(repo, ["act"])
        assert [name for name, _ in linked] == ["act"]
        assert not failures
        assert (mock_env.prefix / "bin" / "act").is_symlink()

        unlinked, _, failures = unlink_packages(repo, ["act"])
        assert "bin/act" in unlinked[0][1].removed
        assert not failures
        assert not (mock_env.prefix / "bin" / "act").exists()

    def test_link_dry_run_leaves_the_prefix_alone(self, repo, mock_env) -> None:
        """Test that a dry run previews the links without creating any."""
        keg = mock_env.cellar / "act" / "0.2.88"
        (keg / "bin").mkdir(parents=True)
        (keg / "bin" / "act").write_text("#!/bin/sh\n")

        linked, _, failures = link_packages(repo, ["act"], dry_run=True)

        assert "bin/act" in linked[0][1].linked
        assert not failures
        assert not (mock_env.prefix / "bin" / "act").exists()

    def test_link_of_a_missing_formula_is_a_failure(self, repo) -> None:
        """Test that an uninstalled name fails rather than silently succeeding."""
        linked, _, failures = link_packages(repo, ["ripgrep"])

        assert linked == []
        assert failures == [("ripgrep", "not installed")]
