"""Integration tests for the install service over a real prefix."""

from __future__ import annotations

import json
import sys

import pytest
from _repo_helpers import _add_alias, _provider_calls, zip_app
from _rows import cask_dict

import brewery.services.cask as cask_service
from brewery.core.models import PackageKind
from brewery.services.install import install_packages


class TestInstall:
    """Tests for the install service."""

    async def test_unbottled_formula_is_not_handed_to_brew(
        self, repo, mock_brew
    ) -> None:
        """Test that a formula with no bottle here fails instead of reaching brew."""
        _installed, failures = await install_packages(
            repo, ["ripgrep"], kind=PackageKind.FORMULA
        )
        assert _provider_calls(mock_brew, "install") == []
        assert [n for n, _ in failures] == ["ripgrep"]

    async def test_install_reports_present_package_as_installed(self, repo) -> None:
        """Test that a package present on the mock fs is reported installed.

        yazi already exists in the mock Cellar, so after the (mocked) install and
        re-scan it is found and returned.
        """
        installed, failures = await install_packages(
            repo, ["yazi"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["yazi"]
        assert failures == []

    async def test_install_reports_absent_package_as_failure(self, repo) -> None:
        """Test that a package absent from the fs after install is a failure.

        The mock does not create the keg, so a never-installed name re-scans as
        missing and is reported as a failure rather than a success.
        """
        installed, failures = await install_packages(
            repo, ["ripgrep"], kind=PackageKind.FORMULA
        )
        assert installed == []
        assert failures == [("ripgrep", "install failed or not found")]

    async def test_install_appearing_package_is_detected(self, repo, mock_env) -> None:
        """Test that a keg created during install is detected on re-scan.

        Simulating brew creating the keg (plus a receipt) makes the package show
        up after invalidation, exercising the cache-invalidate-then-rescan path.
        """
        import orjson

        keg = mock_env.cellar / "ripgrep" / "14.1.0"
        keg.mkdir(parents=True)
        (keg / "INSTALL_RECEIPT.json").write_bytes(
            orjson.dumps({"source": {"tap": "homebrew/core"}})
        )
        installed, failures = await install_packages(
            repo, ["ripgrep"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["ripgrep"]
        assert failures == []

    async def test_install_via_alias_verified_by_canonical_name(self, repo) -> None:
        """Test that an installed alias verifies against its canonical name.

        Requesting "yazi-cli" (an alias for the present "yazi") must report the
        canonical package as installed.
        """
        _add_alias(repo.catalog, "yazi-cli", "yazi")
        installed, failures = await install_packages(
            repo, ["yazi-cli"], kind=PackageKind.FORMULA
        )
        assert [p.name for p in installed] == ["yazi"]
        assert failures == []


@pytest.mark.skipif(sys.platform != "darwin", reason="native casks are macOS-only")
class TestInstallCasks:
    """Tests for routing casks between the native installer and brew."""

    @pytest.fixture
    def appdir(self, tmp_path, monkeypatch):
        """Point brew's appdir away from /Applications, as HOMEBREW_CASK_OPTS can.

        Args:
            tmp_path: The pytest tmp_path fixture.
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            The temporary appdir.
        """
        monkeypatch.setenv("HOMEBREW_CASK_OPTS", f"--appdir={tmp_path / 'apps'}")
        return tmp_path / "apps"

    @pytest.fixture
    def casks(self, repo, mock_env, tmp_path, appdir):
        """Catalog casks over one cached zip: `name` -> its depends_on and artifacts.

        Args:
            repo: The repository fixture.
            mock_env: The mock environment fixture.
            tmp_path: The pytest tmp_path fixture.
            appdir: The temporary appdir fixture.

        Returns:
            A callable writing `{token: (depends_on, artifacts)}` to the catalog.
        """
        sha = zip_app(tmp_path, mock_env.bottle_cache)

        def write(spec: dict) -> None:
            repo.catalog.write_casks(
                [
                    cask_dict(
                        token,
                        version="1.0",
                        url=f"https://example.org/{token}.zip",
                        sha256=sha,
                        depends_on=json.dumps(deps) if deps else None,
                        artifacts=json.dumps(artifacts),
                    )
                    for token, (deps, artifacts) in spec.items()
                ]
            )

        return write

    @staticmethod
    def _receipt(env, token) -> dict:
        """Read a cask's install receipt."""
        return json.loads(
            (env.caskroom / token / ".metadata" / "INSTALL_RECEIPT.json").read_text()
        )

    async def test_native_cask_skips_brew(
        self, repo, mock_brew, mock_env, casks, appdir
    ) -> None:
        """Test that a simple cask is installed natively, honouring --appdir."""
        casks({"foo": (None, [{"app": ["Foo.app"]}])})

        installed, failures = await install_packages(
            repo, ["foo"], kind=PackageKind.CASK
        )

        assert ([p.name for p in installed], failures) == (["foo"], [])
        assert _provider_calls(mock_brew, "install") == []
        assert (appdir / "Foo.app" / "Contents" / "MacOS" / "foo").is_file()
        config = json.loads(
            (mock_env.caskroom / "foo" / ".metadata" / "config.json").read_text()
        )
        assert config["env"] == {"appdir": str(appdir)}
        assert self._receipt(mock_env, "foo")["installed_on_request"] is True

    async def test_only_unsupported_casks_go_to_brew(
        self, repo, mock_brew, casks
    ) -> None:
        """Test that brew gets the pkg cask and the unknown name, and nothing else."""
        casks(
            {
                "foo": (None, [{"app": ["Foo.app"]}]),
                "bar": (None, [{"pkg": ["Bar.pkg"]}]),
            }
        )

        await install_packages(
            repo, ["foo", "bar", "nope", "iina"], kind=PackageKind.CASK
        )

        assert _provider_calls(mock_brew, "install") == [
            ("brew", "install", "--cask", "bar", "nope")
        ]

    async def test_failed_install_is_unstaged_and_goes_to_brew(
        self, repo, mock_brew, mock_env, casks, appdir
    ) -> None:
        """Test that a taken target hands the cask to brew, leaving no Caskroom entry."""
        (appdir / "Foo.app").mkdir(parents=True)
        casks({"foo": (None, [{"app": ["Foo.app"]}])})

        await install_packages(repo, ["foo"], kind=PackageKind.CASK)

        assert _provider_calls(mock_brew, "install") == [
            ("brew", "install", "--cask", "foo")
        ]
        assert not (mock_env.caskroom / "foo").exists()
        assert list((appdir / "Foo.app").iterdir()) == []

    async def test_cask_dependency_is_installed_first(
        self, repo, mock_brew, mock_env, casks, appdir
    ) -> None:
        """Test that a depends_on cask is installed natively, as a dependency."""
        casks(
            {
                "foo": (None, [{"app": ["Foo.app"]}]),
                "baz": (
                    {"cask": ["foo"]},
                    [{"app": ["Foo.app", {"target": "Baz.app"}]}],
                ),
            }
        )

        installed, _ = await install_packages(repo, ["baz"], kind=PackageKind.CASK)

        assert [p.name for p in installed] == ["baz"]
        assert _provider_calls(mock_brew, "install") == []
        assert (appdir / "Baz.app").is_dir()
        assert self._receipt(mock_env, "foo")["installed_as_dependency"] is True
        assert self._receipt(mock_env, "baz")["installed_as_dependency"] is False

    async def test_brew_only_dependency_sends_the_dependent_to_brew(
        self, repo, mock_brew, casks
    ) -> None:
        """Test that brew is given the dependent, and installs the dependency itself."""
        casks(
            {
                "bar": (None, [{"pkg": ["Bar.pkg"]}]),
                "foo": ({"cask": ["bar"]}, [{"app": ["Foo.app"]}]),
            }
        )

        await install_packages(repo, ["foo"], kind=PackageKind.CASK)

        assert _provider_calls(mock_brew, "install") == [
            ("brew", "install", "--cask", "foo")
        ]

    async def test_language_variant_is_installed(
        self, repo, mock_brew, mock_env, casks, appdir, monkeypatch
    ) -> None:
        """Test that `--language` picks the variant's download, version and target."""
        monkeypatch.setenv("HOMEBREW_CASK_OPTS", f"--appdir={appdir} --language=de")
        monkeypatch.setattr(cask_service, "system_languages", lambda: ["en-GB"])
        casks({"foo": (None, [{"app": ["Foo.app"]}])})
        sha = repo.catalog.get_cask("foo").sha256
        repo.catalog.write_casks(
            [
                cask_dict(
                    "foo",
                    version="1.0",
                    url="https://example.org/foo-en.zip",
                    sha256="0" * 64,  # Not cached: fetching it would hand foo to brew
                    artifacts=json.dumps([{"app": ["Foo.app"]}]),
                    language_variations=json.dumps(
                        [
                            {"languages": ["en"], "default": True, "value": "en"},
                            {
                                "languages": ["de"],
                                "default": False,
                                "value": "de",
                                "url": "https://example.org/foo-de.zip",
                                "sha256": sha,
                                "version": "1.0-de",
                                "artifacts": [
                                    {"app": ["Foo.app", {"target": "Foo DE.app"}]}
                                ],
                            },
                        ]
                    ),
                )
            ]
        )

        await install_packages(repo, ["foo"], kind=PackageKind.CASK)

        assert _provider_calls(mock_brew, "install") == []
        assert (appdir / "Foo DE.app").is_dir()
        assert (mock_env.caskroom / "foo" / "1.0-de").is_dir()
        config = json.loads(
            (mock_env.caskroom / "foo" / ".metadata" / "config.json").read_text()
        )
        assert config["env"]["languages"] == ["de"]

    @pytest.mark.parametrize(
        ("formula", "brewed"),
        [
            pytest.param("yazi", [], id="installed"),
            pytest.param(
                "ripgrep", [("brew", "install", "--cask", "foo")], id="unbottled"
            ),
        ],
    )
    async def test_formula_dependency_gates_the_native_install(
        self, repo, mock_brew, mock_env, casks, formula, brewed
    ) -> None:
        """Test that a cask goes to brew only when its formula dependency fails."""
        # A receipt makes the fixture's yazi keg a complete install
        (mock_env.cellar / "yazi" / "26.5.6" / "INSTALL_RECEIPT.json").write_text("{}")
        casks({"foo": ({"formula": [formula]}, [{"app": ["Foo.app"]}])})

        await install_packages(repo, ["foo"], kind=PackageKind.CASK)

        assert _provider_calls(mock_brew, "install") == brewed
