"""Tests the pin/unpin and link/unlink commands' policy and rendering."""

from __future__ import annotations

import pytest

from brewery.cli.commands import link as link_mod
from brewery.cli.commands import pin as pin_mod
from brewery.core.errors import EXIT_USER_ERROR
from brewery.providers.linker import LinkResult, UnlinkResult


@pytest.fixture
def pins(monkeypatch):
    """Stub the pin service's two entry points and record their arguments.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        A callable configuring the three-tuple result and returning the call log.
    """

    def _arrange(done=(), advisories=(), failures=()):
        calls: list[tuple[str, list[str]]] = []

        def _make(verb):
            def _call(repo, names):
                calls.append((verb, names))

                return list(done), list(advisories), list(failures)

            return _call

        monkeypatch.setattr(pin_mod.pin_service, "pin_packages", _make("pin"))
        monkeypatch.setattr(pin_mod.pin_service, "unpin_packages", _make("unpin"))

        return calls

    return _arrange


@pytest.fixture
def links(monkeypatch):
    """Stub the link service's two entry points and record their arguments.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        A callable configuring the three-tuple result and returning the call log.
    """

    def _arrange(done=(), advisories=(), failures=()):
        calls: list[dict] = []

        def _link(repo, names, *, overwrite, force, dry_run):
            calls.append(
                {
                    "verb": "link",
                    "names": names,
                    "overwrite": overwrite,
                    "force": force,
                    "dry_run": dry_run,
                }
            )

            return list(done), list(advisories), list(failures)

        def _unlink(repo, names, *, dry_run):
            calls.append({"verb": "unlink", "names": names, "dry_run": dry_run})

            return list(done), list(advisories), list(failures)

        monkeypatch.setattr(link_mod.link_service, "link_packages", _link)
        monkeypatch.setattr(link_mod.link_service, "unlink_packages", _unlink)

        return calls

    return _arrange


def _result(linked=(), conflicts=()) -> LinkResult:
    """A LinkResult carrying just the fields the previewer reads.

    Args:
        linked: Relative paths that would be symlinked.
        conflicts: (dst, src) pairs that already exist in the prefix.

    Returns:
        A LinkResult.
    """
    return LinkResult(linked=list(linked), conflicts=list(conflicts))


class TestPin:
    """Tests pin and unpin, including the cask rejection."""

    @pytest.mark.parametrize("verb", ["pin", "unpin"])
    def test_a_clean_run_reports_and_exits_zero(self, run, pins, verb) -> None:
        """Test that both verbs forward their names and report the count."""
        calls = pins(done=["wget"])

        result = run([verb, "wget"])

        assert result.exit_code == 0
        assert calls == [(verb, ["wget"])]
        assert f"{verb.capitalize()}ned 1 package(s)" in result.output

    @pytest.mark.parametrize("verb", ["pin", "unpin"])
    def test_casks_are_refused_with_a_brew_suggestion(self, run, pins, verb) -> None:
        """Test that --cask is rejected before anything is opened.

        Brewery currently tracks pin state for formulae only.
        """
        calls = pins()

        result = run([verb, "iina", "--cask"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "not supported" in result.output
        assert f"brew {verb} --cask iina" in result.output
        assert calls == []

    def test_advisories_do_not_change_the_exit_code(self, run, pins) -> None:
        """Test that pinning an already-pinned formula warns and exits 0."""
        pins(done=[], advisories=[("wget", "already pinned")])

        result = run(["pin", "wget"])

        assert result.exit_code == 0
        assert "wget - already pinned" in result.output

    def test_a_failure_exits_user_error(self, run, pins) -> None:
        """Test that a failed pin reports the reason and exits non-zero."""
        pins(failures=[("wget", "not installed")])

        result = run(["pin", "wget"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "wget - not installed" in result.output

    def test_the_success_block_is_suppressed_when_only_failures_remain(
        self, run, pins
    ) -> None:
        """Test that a wholly failed run prints no "Pinned 0 package(s)" line."""
        pins(done=[], failures=[("wget", "not installed")])

        result = run(["pin", "wget"])

        assert "Pinned 0 package(s)" not in result.output


class TestLink:
    """Tests link and unlink, including the dry-run preview."""

    def test_a_clean_link_counts_the_symlinks_created(self, run, links) -> None:
        """Test that the success block names each formula and its symlink count."""
        links(done=[("wget", _result(linked=["bin/wget", "share/man/man1/wget.1"]))])

        result = run(["link", "wget"])

        assert result.exit_code == 0
        assert "Linked 1 package(s)" in result.output
        assert "wget - created 2 symlink(s)" in result.output

    def test_a_dry_run_lists_the_paths_instead_of_the_count(self, run, links) -> None:
        """Test that --dry-run previews the actual paths a real link would make."""
        links(done=[("wget", _result(linked=["bin/wget"]))])

        result = run(["link", "wget", "--dry-run"])

        assert result.exit_code == 0
        assert "Would link 1 path(s) for wget" in result.output
        assert "bin/wget" in result.output
        assert "Linked 1 package(s)" not in result.output

    def test_a_dry_run_also_lists_what_overwrite_would_delete(self, run, links) -> None:
        """Test that conflicting prefix files are shown before they are removed."""
        links(
            done=[
                (
                    "wget",
                    _result(linked=["bin/wget"], conflicts=[("bin/wget", "keg")]),
                )
            ]
        )

        result = run(["link", "wget", "--dry-run", "--overwrite"])

        assert "Would delete 1 existing path(s)" in result.output
        assert "bin/wget" in result.output

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(
                ["link", "wget"],
                {"overwrite": False, "force": False, "dry_run": False},
                id="defaults",
            ),
            pytest.param(
                ["link", "wget", "--overwrite"],
                {"overwrite": True, "force": False, "dry_run": False},
                id="overwrite",
            ),
            pytest.param(
                ["link", "wget", "--force"],
                {"overwrite": False, "force": True, "dry_run": False},
                id="force",
            ),
            pytest.param(
                ["link", "wget", "-n"],
                {"overwrite": False, "force": False, "dry_run": True},
                id="dry_run_short",
            ),
        ],
    )
    def test_the_flags_reach_the_service(self, run, links, argv, expected) -> None:
        """Test that each link flag is forwarded rather than reinterpreted."""
        calls = links(done=[("wget", _result())])

        run(argv)

        assert {k: calls[0][k] for k in expected} == expected

    def test_a_failure_exits_user_error(self, run, links) -> None:
        """Test that a conflicting link reports and exits non-zero."""
        links(failures=[("wget", "bin/wget already exists")])

        result = run(["link", "wget"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "wget - bin/wget already exists" in result.output

    def test_unlink_counts_the_symlinks_removed(self, run, links) -> None:
        """Test that the success block names each formula and its removal count."""
        calls = links(done=[("wget", UnlinkResult(removed=["bin/wget"]))])

        result = run(["unlink", "wget"])

        assert result.exit_code == 0
        assert calls[0] == {"verb": "unlink", "names": ["wget"], "dry_run": False}
        assert "wget - removed 1 symlink(s)" in result.output

    def test_unlink_previews_rather_than_counting_on_a_dry_run(
        self, run, links
    ) -> None:
        """Test that --dry-run lists the paths unlink would remove."""
        calls = links(done=[("wget", UnlinkResult(removed=["bin/wget"]))])

        result = run(["unlink", "wget", "--dry-run"])

        assert result.exit_code == 0
        assert calls[0]["dry_run"] is True
        assert "Would remove 1 path(s) for wget" in result.output
        assert "Unlinked 1 package(s)" not in result.output

    @pytest.mark.parametrize(
        ("alias", "verb"),
        [("ln", "link"), ("ul", "unlink")],
    )
    def test_the_aliases_reach_the_same_commands(self, run, links, alias, verb) -> None:
        """Test that the short forms dispatch to the same bodies."""
        done = _result() if verb == "link" else UnlinkResult()
        calls = links(done=[("wget", done)])

        run([alias, "wget"])

        assert calls[0]["verb"] == verb
