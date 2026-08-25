"""Tests the read-only commands: list, info, search, outdated, cleanup."""

from __future__ import annotations

import pytest

from brewery.cli.commands import cleanup as cleanup_mod
from brewery.cli.commands import outdated as outdated_mod
from brewery.core.errors import EXIT_USER_ERROR, PackageNotFoundError
from brewery.core.models import Package, PackageKind


def _pkg(name: str, version: str = "1.0", latest: str | None = None) -> Package:
    """An installed Package for the renderers.

    Args:
        name: The package name.
        version: The installed version.
        latest: The upstream version, when the package is outdated.

    Returns:
        A Package.
    """
    return Package(
        name=name,
        kind=PackageKind.FORMULA,
        versions=[version],
        metadata={"latest_version": latest} if latest else {},
    )


@pytest.fixture
def installed(monkeypatch, repo):
    """Make `repo.get_all_installed()` report a fixed list.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        repo: The fixture repository.

    Returns:
        A callable taking the packages to report and returning the call log.
    """

    def _arrange(pkgs):
        calls: list[dict] = []

        def _get(self, kind_filter=None):
            calls.append({"kind_filter": kind_filter})

            return list(pkgs)

        monkeypatch.setattr(type(repo), "get_all_installed", _get)

        return calls

    return _arrange


class TestList:
    """Tests the list command's compact and table views."""

    def test_installed_packages_are_listed(self, run, installed) -> None:
        """Test that the compact view names every installed package."""
        installed([_pkg("wget"), _pkg("curl")])

        result = run(["list"])

        assert result.exit_code == 0
        assert "wget" in result.output
        assert "curl" in result.output

    def test_the_kind_filter_is_forwarded(self, run, installed) -> None:
        """Test that --kind reaches the repository rather than filtering after."""
        calls = installed([_pkg("iina")])

        run(["list", "--kind", "cask"])

        assert calls[0]["kind_filter"] is PackageKind.CASK

    def test_rescan_invalidates_the_cache_first(self, run, installed, repo) -> None:
        """Test that --rescan discards the filesystem-state cache before reading.

        Without the invalidate, --rescan would return the same cached answer as
        a plain list and quietly do nothing.
        """
        installed([_pkg("wget")])
        invalidated: list[bool] = []
        repo.cache_mgr.invalidate = lambda: invalidated.append(True)

        result = run(["list", "--rescan"])

        assert result.exit_code == 0
        assert invalidated == [True]

    def test_a_plain_list_does_not_invalidate_the_cache(
        self, run, installed, repo
    ) -> None:
        """Test that the default path stays instant by reading the cache."""
        installed([_pkg("wget")])
        invalidated: list[bool] = []
        repo.cache_mgr.invalidate = lambda: invalidated.append(True)

        run(["list"])

        assert invalidated == []

    def test_the_table_view_paginates_a_long_list(
        self, run, installed, monkeypatch
    ) -> None:
        """Test that more rows than fit the terminal go through the pager.

        page_size is `term_height - 6`, so a short terminal and a long list
        proves the trigger is wired to the real terminal size.
        """
        from brewery.cli import renderers

        installed([_pkg(f"pkg{i}") for i in range(20)])
        monkeypatch.setattr(renderers, "_terminal_size", lambda: (80, 10))
        paged: list[int] = []
        monkeypatch.setattr(
            renderers,
            "paginate",
            lambda *, pkgs, page_size, console: paged.append(page_size),
        )

        result = run(["list", "--table"])

        assert result.exit_code == 0
        assert paged == [4]  # 10 - 6

    def test_the_table_view_prints_directly_when_it_fits(
        self, run, installed, monkeypatch
    ) -> None:
        """Test that a list shorter than one page skips the pager entirely."""
        from brewery.cli import renderers

        installed([_pkg("wget")])
        monkeypatch.setattr(renderers, "_terminal_size", lambda: (80, 40))
        monkeypatch.setattr(
            renderers,
            "paginate",
            lambda **kw: pytest.fail("paginated a single-page list"),
        )

        result = run(["list", "--table"])

        assert result.exit_code == 0
        assert "wget" in result.output

    @pytest.mark.parametrize("alias", ["l", "ls"])
    def test_the_aliases_reach_the_same_command(self, run, installed, alias) -> None:
        """Test that `brewery l` and `brewery ls` are `brewery list`."""
        installed([_pkg("wget")])

        assert run([alias]).exit_code == 0


class TestInfo:
    """Tests the info command."""

    def test_a_known_package_is_described(self, run, monkeypatch, repo) -> None:
        """Test that the details renderer is handed what the repository found."""
        monkeypatch.setattr(
            type(repo), "get_details", lambda self, name, kind: _pkg(name, "1.25.0")
        )

        result = run(["info", "wget"])

        assert result.exit_code == 0
        assert "wget" in result.output

    def test_an_unknown_package_exits_user_error_with_a_suggestion(
        self, run, monkeypatch, repo
    ) -> None:
        """Test that a missing package maps to brew's user-error exit code."""

        def _missing(self, name, kind):
            raise PackageNotFoundError(package=name)

        monkeypatch.setattr(type(repo), "get_details", _missing)

        result = run(["info", "nosuchpkg"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "Package Not Found: nosuchpkg" in result.output
        assert "brewery search nosuchpkg" in result.output


class TestSearch:
    """Tests the search command."""

    def test_matches_are_listed(self, run, monkeypatch, repo) -> None:
        """Test that the search term reaches the repository and results print."""
        seen: list[str] = []

        def _search(self, term):
            seen.append(term)

            return [_pkg("wget")]

        monkeypatch.setattr(type(repo), "search", _search)

        result = run(["search", "wge"])

        assert result.exit_code == 0
        assert seen == ["wge"]
        assert "wget" in result.output

    def test_no_matches_is_not_an_error(self, run, monkeypatch, repo) -> None:
        """Test that an empty search exits 0, as brew does."""
        monkeypatch.setattr(type(repo), "search", lambda self, term: [])

        assert run(["search", "zzz"]).exit_code == 0


class TestOutdated:
    """Tests the outdated command, including its refresh path."""

    def test_nothing_outdated_reports_success(self, run, monkeypatch, repo) -> None:
        """Test that an up-to-date machine says so and exits 0."""
        monkeypatch.setattr(type(repo), "get_outdated", lambda self: [])

        result = run(["outdated"])

        assert result.exit_code == 0
        assert "All packages are up to date" in result.output

    def test_outdated_packages_are_listed_with_the_new_version(
        self, run, monkeypatch, repo
    ) -> None:
        """Test that each row names the package and what it would move to."""
        monkeypatch.setattr(
            type(repo),
            "get_outdated",
            lambda self: [_pkg("wget", "1.24.5", latest="1.25.0")],
        )

        result = run(["outdated"])

        assert "1 outdated package(s)" in result.output
        assert "wget → 1.25.0" in result.output

    def test_the_default_path_does_not_refetch_the_catalog(
        self, run, monkeypatch, repo
    ) -> None:
        """Test that a bare `outdated` stays instant by reading the cache."""
        monkeypatch.setattr(type(repo), "get_outdated", lambda self: [])
        refreshed: list[bool] = []
        monkeypatch.setattr(
            outdated_mod,
            "run_async",
            lambda coro: refreshed.append(True) or coro.close(),
        )

        run(["outdated"])

        assert refreshed == []

    def test_check_refetches_and_rescans_before_filtering(
        self, run, monkeypatch, repo
    ) -> None:
        """Test that --check refreshes the catalog and drops the state cache.

        Refreshing without invalidating would compare fresh upstream versions
        against a stale installed scan, so the two have to happen together.
        """
        monkeypatch.setattr(type(repo), "get_outdated", lambda self: [])
        invalidated: list[bool] = []
        repo.cache_mgr.invalidate = lambda: invalidated.append(True)
        refreshed: list[bool] = []
        monkeypatch.setattr(
            outdated_mod,
            "run_async",
            lambda coro: refreshed.append(True) or coro.close(),
        )

        result = run(["outdated", "--check"])

        assert result.exit_code == 0
        assert refreshed == [True]
        assert invalidated == [True]


class TestCleanup:
    """Tests the cleanup command."""

    @pytest.fixture
    def cleans(self, monkeypatch):
        """Stub cleanup_packages with a fixed (removed, failures) result.

        Args:
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A callable configuring the result.
        """

        def _arrange(removed=(), failures=()):
            async def _cleanup(repo):
                return list(removed), list(failures)

            monkeypatch.setattr(cleanup_mod, "cleanup_packages", _cleanup)

        return _arrange

    def test_an_empty_run_says_there_was_nothing_to_do(self, run, cleans) -> None:
        """Test that a clean machine reports so rather than "Removed 0"."""
        cleans()

        result = run(["cleanup"])

        assert result.exit_code == 0
        assert "Nothing to clean up" in result.output
        assert "Removed 0" not in result.output

    def test_removed_versions_are_listed(self, run, cleans) -> None:
        """Test that each swept keg is named in the result."""
        cleans(removed=["wget 1.24.5", "curl 7.9"])

        result = run(["cleanup"])

        assert result.exit_code == 0
        assert "Removed 2 old version(s)" in result.output
        assert "wget 1.24.5" in result.output

    def test_a_failure_exits_user_error(self, run, cleans) -> None:
        """Test that a keg that could not be removed sets the exit code."""
        cleans(removed=["curl 7.9"], failures=[("wget 1.24.5", "permission denied")])

        result = run(["cleanup"])

        assert result.exit_code == EXIT_USER_ERROR
        assert "wget 1.24.5 - permission denied" in result.output

    @pytest.mark.parametrize("alias", ["c", "clean"])
    def test_the_aliases_reach_the_same_command(self, run, cleans, alias) -> None:
        """Test that `brewery c` and `brewery clean` are `brewery cleanup`."""
        cleans()

        assert run([alias]).exit_code == 0
