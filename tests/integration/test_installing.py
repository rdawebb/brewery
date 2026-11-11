"""Integration tests for the install service over a real prefix."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys

import pytest
from _repo_helpers import _add_alias, _provider_calls, zip_app
from _rows import cask_dict

import brewery.services.cask as cask_service
from brewery.core.locks import lock_path
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

    @pytest.mark.parametrize(
        ("url", "name"),
        [
            pytest.param(
                "https://example.org/r26.1/bin.macosx1015x86_64/p4",
                "bin.macosx1015x86_64",
                id="last_with_extension",
            ),
            pytest.param(
                "https://example.org/cli/1.0.2115+e16833f2/osx-devtunnel",
                "osx-devtunnel",
                id="version_isnt_extension",
            ),
        ],
    )
    async def test_naked_cask_is_copied_as_is(
        self, repo, mock_brew, mock_env, casks, url, name
    ) -> None:
        """Test that a naked download, a zip here, is staged unopened under brew's name."""
        casks({"foo": (None, [])})
        row = cask_dict(
            "foo",
            version="1.0",
            url=url,
            sha256=repo.catalog.get_cask("foo").sha256,
            container=json.dumps({"type": "naked"}),
            artifacts=json.dumps([{"binary": [name, {"target": "foo"}]}]),
        )
        repo.catalog.write_casks([row])

        await install_packages(repo, ["foo"], kind=PackageKind.CASK)

        assert _provider_calls(mock_brew, "install") == []
        staged = mock_env.caskroom / "foo" / "1.0" / name
        assert staged.read_bytes()[:4] == b"PK\x03\x04"
        assert (mock_env.prefix / "bin" / "foo").resolve() == staged.resolve()

    async def test_git_font_cask_fetches_each_font(
        self, repo, mock_brew, mock_env, tmp_path, monkeypatch
    ) -> None:
        """Test that a GitHub font checkout is fetched font by font."""
        fontdir = tmp_path / "fonts"
        monkeypatch.setenv("HOMEBREW_CASK_OPTS", f"--fontdir={fontdir}")
        raw = "https://raw.githubusercontent.com/google/fonts/main/ofl/foo"
        mock_env.bottle_cache.mkdir(parents=True, exist_ok=True)
        for name in ("Foo-Regular.ttf", "Foo%5Bwght%5D.ttf"):
            key = hashlib.sha256(f"{raw}/{name}".encode()).hexdigest()
            (mock_env.bottle_cache / key).write_bytes(name.encode())

        repo.catalog.write_casks(
            [
                cask_dict(
                    "font-foo",
                    version="latest",
                    sha256="no_check",
                    url="https://github.com/google/fonts.git",
                    url_specs=json.dumps({"branch": "main", "only_path": "ofl/foo"}),
                    artifacts=json.dumps(
                        [
                            {"font": ["Foo-Regular.ttf"]},
                            {"font": ["Foo[wght].ttf"]},
                            {"zap": []},
                        ]
                    ),
                )
            ]
        )

        await install_packages(repo, ["font-foo"], kind=PackageKind.CASK)

        assert _provider_calls(mock_brew, "install") == []
        assert (fontdir / "Foo[wght].ttf").read_bytes() == b"Foo%5Bwght%5D.ttf"
        token_dir = mock_env.caskroom / "font-foo"
        staged = token_dir / "latest" / "ofl" / "foo" / "Foo-Regular.ttf"
        assert staged.resolve() == (fontdir / "Foo-Regular.ttf").resolve()
        (caskfile,) = (token_dir / ".metadata").glob("latest/*/Casks/font-foo.json")
        assert json.loads(caskfile.read_text()) == {
            "url_specs": {"only_path": "ofl/foo"}
        }
        assert not (token_dir / ".metadata" / "LATEST_DOWNLOAD_SHA256").exists()

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

    async def test_locked_cask_is_reported_not_handed_to_brew(
        self, repo, mock_brew, mock_env, casks, appdir
    ) -> None:
        """Test that a cask a peer has locked is left alone and reported as locked."""
        casks({"foo": (None, [{"app": ["Foo.app"]}])})
        path = lock_path(mock_env.prefix, "foo", kind="cask")
        path.parent.mkdir(parents=True)

        # Held as a peer process or `brew cleanup` would hold it
        fd = os.open(path, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _, failures = await install_packages(repo, ["foo"], kind=PackageKind.CASK)

        finally:
            os.close(fd)

        assert [(n, "locked" in why) for n, why in failures] == [("foo", True)]
        assert _provider_calls(mock_brew, "install") == []
        assert not (mock_env.caskroom / "foo").exists()
        assert not (appdir / "Foo.app").exists()

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
