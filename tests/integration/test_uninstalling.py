"""Integration tests for the uninstall service over a real prefix."""

from __future__ import annotations

from types import SimpleNamespace

from _repo_helpers import _add_alias, _install_formula, _provider_calls

from brewery.core.models import PackageKind
from brewery.core.repo import Repository
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

        async def mock_cask_uninstall(names) -> list[str]:
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
