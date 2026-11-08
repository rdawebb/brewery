"""Integration tests for the uninstall service over a real prefix."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from _repo_helpers import _add_alias, _install_formula, _provider_calls, zip_app
from _rows import cask_dict

import brewery.providers.cask_directives as directives
from brewery.core.models import PackageKind
from brewery.core.repo import Repository
from brewery.providers import caskroom
from brewery.services.install import install_packages
from brewery.services.uninstall import _verify_removed, uninstall_packages


class TestUninstall:
    """Tests for the uninstall service."""

    async def test_uninstall_still_present_is_failure(self, repo, monkeypatch) -> None:
        """Test that a package still on disk after native & fallback uninstall is a failure.

        The mock does not delete the keg, so _verify_removed sees it still present
        and reports failure rather than a phantom success.
        """
        import brewery.services.uninstall as svc

        def _boom(*a, **k) -> None:
            """Raise OSError to simulate native uninstall failure.

            Args:
                *a: Positional arguments
                **k: Keyword arguments

            Raises:
                OSError: Always raised to simulate native uninstall failure
            """
            raise OSError("native failed")

        monkeypatch.setattr(svc, "remove_rack", _boom)

        # mock_brew logs but does not delete the keg, so _verify_removed sees it
        removed, failures = await uninstall_packages(
            repo, ["yazi"], kind=PackageKind.FORMULA
        )
        assert removed == []
        assert failures == [("yazi", "uninstall failed")]

    async def test_uninstall_removed_package_is_success(self, repo, mock_env) -> None:
        """Test that a keg removed during uninstall verifies as removed."""
        import shutil

        shutil.rmtree(mock_env.cellar / "yazi")
        removed, failures = await uninstall_packages(
            repo, ["yazi"], kind=PackageKind.FORMULA
        )
        assert "yazi" in removed
        assert failures == []

    async def test_unknown_kind_resolves_via_installed(self, catalog, mock_env) -> None:
        """Test that kind=None resolves each name's kind from installed state and
        routes them to the correct backend: formula -> native, cask -> provider"""
        import shutil

        async def mock_cask_uninstall(names, *, zap=False) -> list[str]:
            """Simulate brew uninstall removing the keg during the operation.

            Args:
                names: The names to operate on.

            Returns:
                The names unchanged.
            """
            for name in names:
                shutil.rmtree(mock_env.caskroom / name, ignore_errors=True)

            return names

        removed, failures = await uninstall_packages(
            Repository(catalog=catalog),
            ["yazi", "iina"],
            cask=SimpleNamespace(uninstall=mock_cask_uninstall),
        )
        assert len(removed) == 2
        assert failures == []

    async def test_unknown_kind_not_installed_is_not_found(self, repo) -> None:
        """Test that an uninstall target that is not installed is 'not found'."""
        removed, failures = await uninstall_packages(repo, ["ripgrep"])
        assert removed == []
        assert failures == [("ripgrep", "not found")]

    async def test_uninstall_via_alias_resolves_to_canonical(
        self, catalog, mock_env
    ) -> None:
        """Test that an alias is resolved before kind routing and verification.

        Uninstalling "yazi-cli" (an alias for installed "yazi") must route the
        canonical name to the backend and verify its keg, reporting "yazi" removed.
        """
        _add_alias(catalog, "yazi-cli", "yazi")
        removed, failures = await uninstall_packages(
            Repository(catalog=catalog), ["yazi-cli"]
        )
        assert removed == ["yazi"]
        assert failures == []

    async def test_uninstall_routes_formula_native_and_cask_providers(
        self, repo, mock_brew, mock_env
    ) -> None:
        """Test that formulae removed natively, casks routed to brew."""
        await uninstall_packages(repo, ["yazi", "iina"])
        assert not (mock_env.cellar / "yazi").exists()  # Formula: native
        flat = [a for c in _provider_calls(mock_brew, "uninstall") for a in c]
        assert "iina" in flat  # Cask: brew provider
        assert "yazi" not in flat  # Formula should not hit brew

    async def test_uninstall_blocked_by_dependent(self, repo, mock_env) -> None:
        """Test that a formula required by another installed formula is refused."""
        _install_formula(mock_env.cellar, "openssl")
        _install_formula(mock_env.cellar, "curl", deps=["openssl"])
        repo.cache_mgr.invalidate()
        removed, failures = await uninstall_packages(
            repo, ["openssl"], kind=PackageKind.FORMULA
        )
        assert removed == []
        assert failures == [("openssl", "required by curl")]
        assert (mock_env.cellar / "openssl").exists()

    async def test_uninstall_both_in_batch_unblocks(self, repo, mock_env) -> None:
        """Test that a dependent removed in the same batch does not block the target."""
        _install_formula(mock_env.cellar, "openssl")
        _install_formula(mock_env.cellar, "curl", deps=["openssl"])
        repo.cache_mgr.invalidate()
        removed, failures = await uninstall_packages(
            repo, ["openssl", "curl"], kind=PackageKind.FORMULA
        )
        assert len(removed) == 2
        assert failures == []
        assert not (mock_env.cellar / "openssl").exists()

    async def test_pinned_formula_is_refused_and_still_blocks(
        self, repo, mock_env
    ) -> None:
        """Test that a pinned formula is kept, and so is what it depends on."""
        _install_formula(mock_env.cellar, "openssl")
        _install_formula(mock_env.cellar, "curl", deps=["openssl"])
        pin = mock_env.prefix / "var/homebrew/pinned/curl"
        pin.parent.mkdir(parents=True)
        pin.symlink_to(mock_env.cellar / "curl")
        repo.cache_mgr.invalidate()
        removed, failures = await uninstall_packages(
            repo, ["openssl", "curl"], kind=PackageKind.FORMULA
        )
        assert removed == []
        assert failures == [
            ("curl", "pinned, unpin with `brewery unpin`"),
            ("openssl", "required by curl"),
        ]
        assert (mock_env.cellar / "curl").exists()
        assert (mock_env.cellar / "openssl").exists()

    async def test_uninstall_lists_multiple_dependents(self, repo, mock_env) -> None:
        """Test that multiple dependents are reported sorted and comma-joined."""
        _install_formula(mock_env.cellar, "openssl")
        _install_formula(mock_env.cellar, "curl", deps=["openssl"])
        _install_formula(mock_env.cellar, "wget", deps=["openssl"])
        repo.cache_mgr.invalidate()
        _, failures = await uninstall_packages(
            repo, ["openssl"], kind=PackageKind.FORMULA
        )
        assert failures == [("openssl", "required by curl, wget")]

    async def test_uninstall_removes_keg_natively(
        self, repo, mock_brew, mock_env
    ) -> None:
        """Test that Formula uninstall removes the keg via the native path, not brew."""
        removed, _ = await uninstall_packages(repo, ["yazi"], kind=PackageKind.FORMULA)
        assert "yazi" in removed
        assert not (mock_env.cellar / "yazi").exists()
        assert _provider_calls(mock_brew, "uninstall") == []

    async def test_uninstall_falls_back_to_brew(
        self, repo, mock_brew, monkeypatch
    ) -> None:
        """Test that a native failure falls back to brew uninstall for that formula."""
        import brewery.services.uninstall as svc

        def _boom(*a, **k) -> None:
            """Raise OSError to simulate native uninstall failure.

            Args:
                *a: Positional arguments
                **k: Keyword arguments

            Raises:
                OSError: Always raised to simulate native uninstall failure
            """
            raise OSError("native failed")

        monkeypatch.setattr(svc, "remove_rack", _boom)
        await uninstall_packages(repo, ["yazi"], kind=PackageKind.FORMULA)
        assert _provider_calls(mock_brew, "uninstall")


@pytest.mark.skipif(sys.platform != "darwin", reason="native casks are macOS-only")
class TestUninstallCasks:
    """Tests for uninstalling casks natively, round-tripping a native install."""

    @pytest.fixture
    def install(self, repo, mock_env, tmp_path, monkeypatch):
        """Install `foo` natively with the given extra stanzas, into a temp appdir.

        Args:
            repo: The repository fixture.
            mock_env: The mock environment fixture.
            tmp_path: The pytest tmp_path fixture.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            An async callable taking the extra artifact stanzas.
        """
        monkeypatch.setenv("HOMEBREW_CASK_OPTS", f"--appdir={tmp_path / 'apps'}")
        monkeypatch.setattr(directives, "_running_apps", list)  # Nothing to quit
        sha = zip_app(tmp_path, mock_env.bottle_cache)

        async def run(*stanzas: dict) -> None:
            artifacts = [
                {"app": ["Foo.app"]},
                {"binary": ["$APPDIR/Foo.app/Contents/MacOS/foo"]},
                *stanzas,
            ]
            repo.catalog.write_casks(
                [
                    cask_dict(
                        "foo",
                        version="1.0",
                        url="https://example.org/foo.zip",
                        sha256=sha,
                        artifacts=json.dumps(artifacts),
                    )
                ]
            )
            await install_packages(repo, ["foo"], kind=PackageKind.CASK)
            assert (tmp_path / "apps" / "Foo.app").is_dir()

        return run

    async def test_native_uninstall(self, repo, mock_brew, mock_env, install, tmp_path):
        """Test that the directives run, the artifacts go and so does the Caskroom dir."""
        leftover, prefs = tmp_path / "leftover", tmp_path / "prefs"
        leftover.write_text("")
        prefs.write_text("")
        await install(
            {"uninstall": [{"quit": "org.example.foo", "delete": str(leftover)}]},
            {"zap": [{"trash": str(prefs)}]},
        )

        removed, failures = await uninstall_packages(
            repo, ["foo"], kind=PackageKind.CASK
        )

        assert (removed, failures) == (["foo"], [])
        assert _provider_calls(mock_brew, "uninstall") == []
        assert not (tmp_path / "apps" / "Foo.app").exists()
        assert not (mock_env.prefix / "bin" / "foo").is_symlink()
        assert not (mock_env.caskroom / "foo").exists()
        assert not leftover.exists()
        assert prefs.exists()  # `zap` only runs for --zap

    async def test_zap(self, repo, mock_brew, mock_env, install, tmp_path, monkeypatch):
        """Test that --zap trashes the files and removes dirs left empty."""
        trashed: list[list[str]] = []
        monkeypatch.setitem(directives._RUNNERS, "trash", trashed.append)
        support = tmp_path / "support"
        (support / "sub").mkdir(parents=True)
        (support / ".DS_Store").write_text("")
        await install(
            {"zap": [{"trash": str(tmp_path / "prefs"), "rmdir": str(support)}]}
        )

        removed, _ = await uninstall_packages(
            repo, ["foo"], kind=PackageKind.CASK, zap=True
        )

        assert removed == ["foo"]
        assert _provider_calls(mock_brew, "uninstall") == []
        assert trashed == [[str(tmp_path / "prefs")]]
        assert not support.exists()
        assert not (mock_env.caskroom / "foo").exists()

    async def test_zap_brewery_cant_run_goes_to_brew(
        self, repo, mock_brew, install, tmp_path
    ) -> None:
        """Test that a `zap` with a `pkgutil` hands the whole --zap to brew."""
        await install({"zap": [{"pkgutil": "org.example.foo", "trash": "~/x"}]})

        await uninstall_packages(repo, ["foo"], kind=PackageKind.CASK, zap=True)

        assert _provider_calls(mock_brew, "uninstall") == [
            ("brew", "uninstall", "--cask", "--zap", "foo")
        ]
        assert (tmp_path / "apps" / "Foo.app").is_dir()  # brew wasn't really run

    async def test_failure_goes_to_brew(
        self, repo, mock_brew, install, monkeypatch
    ) -> None:
        """Test that a cask whose native uninstall fails part way goes to brew."""
        await install()

        def refuse(path) -> None:
            raise PermissionError(f"can't remove {path}")

        monkeypatch.setattr(caskroom, "remove", refuse)
        await uninstall_packages(repo, ["foo"], kind=PackageKind.CASK)

        assert _provider_calls(mock_brew, "uninstall") == [
            ("brew", "uninstall", "--cask", "foo")
        ]

    async def test_pinned_is_refused(self, repo, mock_brew, mock_env, install):
        """Test that a pinned cask is refused, not handed to brew to refuse."""
        await install()
        pin = mock_env.prefix / "var/homebrew/pinned_casks/foo"
        pin.parent.mkdir(parents=True)
        pin.symlink_to(mock_env.caskroom / "foo" / "1.0")

        removed, failures = await uninstall_packages(
            repo, ["foo"], kind=PackageKind.CASK
        )

        assert removed == []
        assert failures == [("foo", "pinned, unpin with `brew unpin --cask`")]
        assert _provider_calls(mock_brew, "uninstall") == []
        assert (mock_env.caskroom / "foo").is_dir()


class TestVerifyRemoved:
    """Tests for _verify_removed's definition of "still installed"."""

    def test_rack_holding_a_keg_is_a_failure(self, repo, mock_env) -> None:
        """Test that a formula whose rack still holds a keg counts as not removed."""
        assert _verify_removed(["yazi"], PackageKind.FORMULA, env=mock_env) == (
            [],
            ["yazi"],
        )

    def test_absent_rack_is_removed(self, repo, mock_env) -> None:
        """Test that a formula with no rack at all counts as removed."""
        assert _verify_removed(["ripgrep"], PackageKind.FORMULA, env=mock_env) == (
            ["ripgrep"],
            [],
        )

    def test_emptied_rack_is_removed_and_pruned(self, repo, mock_env) -> None:
        """Test that a rack left behind holding no keg counts as removed.

        `fs_state` reports a rack with no keg directory as not installed, so
        reporting it as an uninstall failure would contradict the next `list`.
        """
        import shutil

        rack = mock_env.cellar / "yazi"
        shutil.rmtree(rack / "26.5.6")

        assert _verify_removed(["yazi"], PackageKind.FORMULA, env=mock_env) == (
            ["yazi"],
            [],
        )

        # The empty shell is swept up, so the tree agrees with the verdict
        assert not rack.exists()

    def test_cask_token_matches_case_insensitively(self, repo, mock_env) -> None:
        """Test that a differently-cased cask token still finds its Caskroom dir."""
        assert _verify_removed(["IINA"], PackageKind.CASK, env=mock_env) == (
            [],
            ["IINA"],
        )

    def test_cask_token_with_only_metadata_is_removed(self, repo, mock_env) -> None:
        """Test that a token directory holding no version directory counts as removed."""
        import shutil

        token = mock_env.caskroom / "iina"
        shutil.rmtree(token / "1.4.1,160")
        (token / ".metadata").mkdir(exist_ok=True)

        assert _verify_removed(["iina"], PackageKind.CASK, env=mock_env) == (
            ["iina"],
            [],
        )
