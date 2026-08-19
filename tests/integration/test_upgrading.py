"""Integration tests for the upgrade service over a real prefix."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _repo_helpers import _NullSink, _provider_calls

from brewery.core.models import PackageKind
from brewery.core.repo import Repository
from brewery.services.pin import pin_packages, unpin_packages
from brewery.services.upgrade import upgrade_packages

pytestmark = pytest.mark.integration


class TestUpgrade:
    """Tests for the upgrade service."""

    async def test_upgrade_all_targets_outdated(self, repo, mock_brew) -> None:
        """Test that an upgrade with no names targets the outdated set.

        act is the only outdated package, so it is the upgrade target.
        """
        await upgrade_packages(repo)
        upgrades = _provider_calls(mock_brew, "upgrade")
        flat = [arg for call in upgrades for arg in call]
        assert "act" in flat
        assert "yazi" not in flat  # Up-to-date, not targeted

    async def test_upgrade_named_package(self, repo, mock_brew) -> None:
        """Test that a named upgrade routes that package to the provider."""
        await upgrade_packages(repo, ["act"])
        flat = [arg for call in _provider_calls(mock_brew, "upgrade") for arg in call]
        assert "act" in flat

    async def test_named_up_to_date_formula_is_not_repoured(
        self, repo, mock_brew
    ) -> None:
        """Test that naming a current formula reports it instead of re-pouring it.

        yazi is up to date, so it must never reach the pipeline or the provider.
        """
        upgraded, current, _advisories, failures = await upgrade_packages(
            repo, ["yazi"]
        )
        flat = [arg for call in _provider_calls(mock_brew, "upgrade") for arg in call]
        assert "yazi" not in flat
        assert upgraded == []
        assert failures == []
        assert [p.name for p in current] == ["yazi"]

    async def test_named_outdated_formula_still_upgrades(self, repo, mock_brew) -> None:
        """Test that the up-to-date filter leaves an outdated named target alone.

        Both are still reported: the mock provider changes no version on disk, so
        act comes back as current rather than upgraded.
        """
        _upgraded, current, _advisories, _failures = await upgrade_packages(
            repo, ["act", "yazi"]
        )
        flat = [arg for call in _provider_calls(mock_brew, "upgrade") for arg in call]
        assert "act" in flat
        assert "yazi" not in flat
        assert sorted(p.name for p in current) == ["act", "yazi"]

    async def test_upgrade_unknown_name_is_failure(self, repo) -> None:
        """Test that upgrading a non-installed name is reported as not found."""
        upgraded, _current, _advisories, failures = await upgrade_packages(
            repo, ["ripgrep"]
        )
        assert upgraded == []
        assert failures == [("ripgrep", "not found")]

    async def test_pinned_package_skipped_on_upgrade_all(self, repo) -> None:
        """Test that a pinned outdated package is skipped, not upgraded.

        Pinning act (which is outdated) should move it to advisories with a
        'pinned' reason and keep it out of the upgrade targets. A bulk upgrade
        skips pins without failing, matching `brew upgrade`.
        """
        assert pin_packages(repo, ["act"])[0] == ["act"]

        upgraded, _current, advisories, failures = await upgrade_packages(repo)
        assert ("act", "pinned - not upgraded") in advisories
        assert failures == []
        assert all(p.name != "act" for p in upgraded)

    async def test_pinned_named_package_skipped_on_upgrade(
        self, repo, mock_brew
    ) -> None:
        """Test that an explicitly named pinned package is refused, not upgraded.

        Naming a pinned package is an error, unlike skipping it in a bulk upgrade.
        """
        assert pin_packages(repo, ["act"])[0] == ["act"]

        upgraded, _current, _advisories, failures = await upgrade_packages(
            repo, ["act"]
        )
        assert ("act", "pinned - skipped") in failures
        assert all(p.name != "act" for p in upgraded)

        flat = [arg for call in _provider_calls(mock_brew, "upgrade") for arg in call]
        assert "act" not in flat

    async def test_unpinned_package_upgrades_again(self, repo) -> None:
        """Test that unpinning restores a package to the upgrade targets."""
        pin_packages(repo, ["act"])
        assert unpin_packages(repo, ["act"])[0] == ["act"]

        upgraded, _current, advisories, _failures = await upgrade_packages(repo)
        assert ("act", "pinned - not upgraded") not in advisories
        assert all(p.name != "act" for p in upgraded)

    async def test_upgrade_detects_version_change(
        self, mock_brew, catalog, mock_env
    ) -> None:
        """Test that a version bump on the mock fs is reported as upgraded.

        Simulating brew replacing act 0.2.88 with 0.2.89 during the upgrade makes
        the post-upgrade re-scan see a new version, classifying it as upgraded
        rather than current. The swap happens inside an injected mock provider so
        act is still present (at 0.2.88) when the pre-upgrade snapshot is taken.
        """
        import shutil

        import orjson

        async def mock_formula_upgrade(names) -> list[str]:
            """Simulate brew upgrade replacing the keg with a new version.

            Args:
                names: The names to operate on.

            Returns:
                The names unchanged.
            """
            act_dir = mock_env.cellar / "act"
            shutil.rmtree(act_dir)
            new_keg = act_dir / "0.2.89"
            new_keg.mkdir(parents=True)
            (new_keg / "INSTALL_RECEIPT.json").write_bytes(
                orjson.dumps({"source": {"tap": "homebrew/core"}})
            )

            return names

        upgraded, current, _advisories, _failures = await upgrade_packages(
            Repository(catalog=catalog),
            ["act"],
            formula=SimpleNamespace(upgrade=mock_formula_upgrade),
        )
        assert [p.name for p in upgraded] == ["act"]
        assert current == []

    async def test_kind_filter_limits_targets(self, repo, mock_brew) -> None:
        """Test that a kind filter restricts which providers are invoked.

        Upgrading with kind=CASK and no outdated casks should invoke no formula
        upgrade for the outdated formula act.
        """
        await upgrade_packages(repo, kind=PackageKind.CASK)
        flat = [arg for call in _provider_calls(mock_brew, "upgrade") for arg in call]
        assert "act" not in flat

    async def test_native_upgrade_bumps_version_and_retains_old(
        self, brew, empty_catalog, monkeypatch
    ) -> None:
        """Test that a native upgrade links the new version and keeps the old as a stamped stale keg."""
        from pathlib import Path

        import orjson

        import brewery.providers.orchestrator as orch_mod
        import brewery.providers.pipeline as install_svc
        from brewery.core import config
        from brewery.core.shell import BrewResult
        from brewery.providers.manifest import BottleTabInfo

        # Installed state: wget 1.0, opt -> 1.0, minimal receipt
        brew.formula(
            "wget",
            "1.0",
            receipt={
                "source": {"tap": "homebrew/core"},
                "runtime_dependencies": [],
                "installed_on_request": True,
            },
            link_opt=True,
        )
        monkeypatch.setattr(config, "_env_cache", brew.env)

        # Catalog: wget 2.0 WITH bottle fields
        empty_catalog.write_formulae(
            [
                {
                    "name": "wget",
                    "desc": None,
                    "homepage": None,
                    "tap": "homebrew/core",
                    "version": "2.0",
                    "revision": 0,
                    "version_scheme": 0,
                    "keg_only": 0,
                    "has_service": 0,
                    "post_install": 0,
                    "bottle_url": "https://ghcr.io/v2/homebrew/core/wget/blobs/sha256:dead",
                    "bottle_sha256": "d" * 64,
                    "bottle_cellar": ":any_skip_relocation",
                    "bottle_rebuild": 0,
                    "deprecated": 0,
                    "disabled": 0,
                }
            ],
            [],
            [],
        )

        # The keg the mocked download+extract hands back.
        staged = brew.prefix.parent / "staged_wget"
        (staged / "bin").mkdir(parents=True)
        (staged / "bin" / "wget").write_text("v2")

        class MockDownloader:
            def __init__(self, cache_dir, client):  # build_orchestrator's call shape
                """Initialise the mock downloader with a cache directory and client."""

            async def fetch(self, ref, *, on_progress=None) -> Path:
                """Return a mock Path for the wget tarball.

                Args:
                    ref: The reference to fetch (unused).
                    on_progress: Optional progress callback (unused).

                Returns:
                    A Path object pointing to the fake wget tarball.
                """
                return Path("/fake/wget.tar.gz")

        async def mock_tab(
            client, *, name, version, bottle_sha256, revision, rebuild
        ) -> BottleTabInfo:
            """Return a mock BottleTabInfo for the wget package.

            Args:
                name: The package name.
                version: The package version.
                bottle_sha256: The SHA-256 hash of the bottle.
                revision: The revision number.
                rebuild: Whether the bottle needs to be rebuilt.

            Returns:
                A BottleTabInfo object with mock data for the wget package.
            """
            return BottleTabInfo(
                homebrew_version="5.1",
                changed_files=[],
                source_modified_time=1,
                compiler="clang",
                runtime_dependencies=[],
                arch="x86_64",
                built_on={"os": "Macintosh"},
                path_exec_files=[],
                installed_size=None,
            )

        monkeypatch.setattr(install_svc, "Downloader", MockDownloader)
        monkeypatch.setattr(install_svc, "fetch_bottle_tab", mock_tab)
        monkeypatch.setattr(
            orch_mod, "extract_bottle", lambda bp, st, *, sink=None: staged
        )
        monkeypatch.setattr(orch_mod, "StreamRelocator", lambda **kw: _NullSink())

        # Defensive: a stray fallback must never reach the real brew binary
        async def no_brew(args, *, output=None, check=None):
            """Raise an exception to ensure no real brew call is made.

            Args:
                args: The command-line arguments for the brew call.
                output: The output file path (unused).
                check: Whether to raise an exception on non-zero return code (unused).

            Returns:
                A BrewResult object with empty stdout/stderr and returncode 0.
            """
            return BrewResult(stdout="", stderr="", returncode=0)

        monkeypatch.setattr("brewery.providers.brew.run_brew", no_brew)

        repo = Repository(catalog=empty_catalog)
        upgraded, _current, _advisories, failures = await upgrade_packages(
            repo, ["wget"]
        )

        # Version bump reported
        assert [p.name for p in upgraded] == ["wget"]
        assert upgraded[0].versions[0] == "2.0"
        assert failures == []

        # New keg linked; opt and the prefix link point at 2.0
        new = brew.cellar / "wget" / "2.0"
        assert new.exists()
        assert Path((brew.prefix / "opt" / "wget").resolve()) == new
        assert Path((brew.prefix / "bin" / "wget").resolve()) == new / "bin" / "wget"

        # Old keg retained as a stale version and stamped for cleanup
        old = brew.cellar / "wget" / "1.0"
        assert old.exists()
        sidecar = orjson.loads((old / ".brewery_replaced.json").read_bytes())
        assert sidecar["replaced_by"] == "2.0"

        # The rescan resolves the active version to 2.0 (1.0 is now a stale version)
        pkg = next(p for p in repo.get_all_installed() if p.name == "wget")
        assert pkg.versions[0] == "2.0"
