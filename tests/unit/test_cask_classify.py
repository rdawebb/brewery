"""Unit tests for routing casks to the native installer or to brew."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from _rows import cask_row

from brewery import __version__
from brewery.core.cask_locale import localise, parse_locale, pick_language
from brewery.core.host import Platform
from brewery.providers import cask
from brewery.providers.cask import (
    classify,
    classify_uninstall,
    git_files,
    read_cask_opts,
    url_headers,
)

SEQUOIA = Platform(arch="arm64", os="macos", macos_major=15)


class TestClassify:
    """Tests for classify."""

    def test_fixture_cask_is_native(self, catalog) -> None:
        """Test that iina (app, binary, uninstall quit, zap) installs natively."""
        assert classify(catalog.get_cask("iina"), SEQUOIA) == ("native", "")

    @pytest.mark.parametrize(
        ("artifacts", "reason"),
        [
            pytest.param([{"pkg": ["Foo.pkg"]}], "`pkg` artifact", id="pkg"),
            pytest.param(
                [{"installer": [{"manual": "Install.app"}]}],
                "`installer` artifact",
                id="installer",
            ),
            pytest.param(
                [{"app": ["Foo.app"]}, {"postflight_steps": None}],
                "`postflight_steps` artifact",
                id="postflight_steps",
            ),
            pytest.param(
                [{"command_wrapper": ["foo"]}],
                "`command_wrapper` artifact",
                id="command_wrapper",
            ),
            pytest.param(
                [{"app": ["Foo.app"]}, {"uninstall": [{"quit": "a"}, {"script": "b"}]}],
                "`uninstall script`",
                id="uninstall_script",
            ),
        ],
    )
    def test_foreign_artifacts_go_to_brew(self, artifacts, reason) -> None:
        """Test that anything outside the native artifact set goes to brew."""
        assert classify(cask_row(artifacts=artifacts), SEQUOIA) == ("brew", reason)

    @pytest.mark.parametrize(
        ("depends_on", "expected"),
        [
            pytest.param({"macos": {">=": ["15"]}}, ("native", ""), id="macos_met"),
            pytest.param(
                {"macos": {">=": ["26"]}},
                ("brew", "needs macOS >= 26"),
                id="macos_too_new",
            ),
            pytest.param(
                {"maximum_macos": {"<=": ["14"]}},
                ("brew", "needs macOS <= 14"),
                id="macos_too_old",
            ),
            pytest.param(
                {"macos": {"==": ["13", "14"]}},
                ("brew", "needs macOS == 13, 14"),
                id="macos_not_listed",
            ),
            pytest.param(
                {"arch": [{"type": "intel", "bits": 64}]},
                ("brew", "needs amd64"),
                id="wrong_arch",
            ),
            pytest.param({"formula": ["wget"]}, ("native", ""), id="formula_dep"),
        ],
    )
    def test_depends_on(self, depends_on, expected) -> None:
        """Test that a cask this host can't run goes to brew, which refuses it."""
        row = cask_row(artifacts=[{"app": ["Foo.app"]}], depends_on=depends_on)
        assert classify(row, SEQUOIA) == expected

    @pytest.mark.parametrize(
        ("fields", "reason"),
        [
            pytest.param(
                {"container": {"nested": "Foo.dmg"}},
                "`container` unpacking",
                id="nested",
            ),
            pytest.param(
                {"container": {"nested": "Foo.tar.gz", "type": "zip"}},
                "`container` unpacking",
                id="nested_zip",
            ),
            pytest.param(
                {"url_specs": {"branch": "main", "only_path": "fonts"}},
                "`url` git checkout",
                id="git",
            ),
            pytest.param(
                {"url_specs": {"referer": "https://a/", "header": "X-A: b"}},
                "`url header:`",
                id="header",
            ),
            pytest.param(
                {"url_specs": {"using": "post", "data": {"a": "b"}}},
                "`url data:`",
                id="post",
            ),
            pytest.param(
                {"language_variations": [{"languages": ["de"], "default": False}]},
                "`language` variant",
                id="languages",
            ),
        ],
    )
    def test_download_brew_alone_can_fetch(self, fields, reason) -> None:
        """Test that a download only brew can fetch or unpack goes to brew up front."""
        row = cask_row(artifacts=[{"app": ["Foo.app"]}], **fields)
        assert classify(row, SEQUOIA) == ("brew", reason)

    @pytest.mark.parametrize(
        "specs",
        [
            {"user_agent": ":fake", "cookies": {"a": "b"}},
            {"user_agent": "Foo/1.0", "referer": "https://a/"},
            {"user_agent": ":curl"},
            {"user_agent": ":default"},
        ],
    )
    def test_header_specs_are_native(self, specs) -> None:
        """Test that the `url` options sent as headers keep the cask native."""
        row = cask_row(artifacts=[{"app": ["Foo.app"]}], url_specs=specs)
        assert classify(row, SEQUOIA) == ("native", "")

    @pytest.mark.parametrize("kind", ["naked", "dmg", "zip"])
    def test_container_type_is_native(self, kind) -> None:
        """Test that a container `stage` copies or sniffs keeps the cask native."""
        row = cask_row(artifacts=[{"binary": ["p4"]}], container={"type": kind})
        assert classify(row, SEQUOIA) == ("native", "")

    def test_github_font_checkout_is_native(self) -> None:
        """Test that fonts sparse-checked-out of a GitHub repo keep the cask native."""
        row = cask_row(
            url="https://github.com/google/fonts.git",
            url_specs={"branch": "main", "only_path": "ofl/abeezee"},
            artifacts=[{"font": ["ABeeZee-Regular.ttf"], "target": "x"}, {"zap": []}],
        )
        assert classify(row, SEQUOIA) == ("native", "")

    def test_off_macos_goes_to_brew(self) -> None:
        """Test that native casks are macOS-only."""
        linux = Platform(arch="amd64", os="linux")
        assert classify(cask_row(artifacts=[{"font": ["a.ttf"]}]), linux)[0] == "brew"


class TestGitFiles:
    """Tests for git_files."""

    def test_raw_url_per_font(self) -> None:
        """Test that each font maps from brew's checkout path to its quoted raw URL."""
        row = cask_row(
            url="https://github.com/google/fonts.git",
            url_specs={"branch": "main", "only_path": "ofl/advent pro"},
            artifacts=[{"font": ["AdventPro[wdth,wght].ttf"]}, {"zap": []}],
        )
        assert git_files(row) == {
            "ofl/advent pro/AdventPro[wdth,wght].ttf": "https://raw.githubusercontent.com"
            "/google/fonts/main/ofl/advent%20pro/AdventPro%5Bwdth%2Cwght%5D.ttf"
        }

    def test_branch_defaults_to_master(self) -> None:
        """Test that brew's default ref is used when only `only_path` is given."""
        row = cask_row(
            url="https://github.com/powerline/fonts",
            url_specs={"only_path": "FiraMono"},
            artifacts=[{"font": ["a.otf"]}],
        )
        assert git_files(row) == {
            "FiraMono/a.otf": "https://raw.githubusercontent.com/powerline/fonts/master/FiraMono/a.otf"
        }

    @pytest.mark.parametrize(
        ("url", "artifacts"),
        [
            pytest.param(
                "https://gitlab.com/a/b.git", [{"font": ["a.ttf"]}], id="not_github"
            ),
            pytest.param(
                "https://github.com/a/b.git", [{"app": ["A.app"]}], id="not_a_font"
            ),
            pytest.param(
                "https://github.com/a/b.git", [{"font": ["../a.ttf"]}], id="escapes"
            ),
            pytest.param("https://github.com/a/b.git", [{"zap": []}], id="no_fonts"),
        ],
    )
    def test_brew_checks_out_the_rest(self, url, artifacts) -> None:
        """Test that a checkout brewery can't fetch file by file goes to brew."""
        row = cask_row(url=url, url_specs={"only_path": "x"}, artifacts=artifacts)
        assert git_files(row) == {}
        assert classify(row, SEQUOIA) == ("brew", "`url` git checkout")


_LANG = ("Accept-Language", "en")
_DEFAULT_AGENT = ("User-Agent", f"brewery/{__version__} curl/8.7.1")


class TestUrlHeaders:
    """Tests for the request headers a cask's `url` options ask for."""

    @pytest.fixture(autouse=True)
    def _curl(self, monkeypatch) -> None:
        """Patch the curl agent to a known value so tests don't depend on the host."""
        monkeypatch.setattr(cask, "_curl_agent", lambda: "curl/8.7.1")

    @pytest.mark.parametrize(
        ("specs", "expected"),
        [
            pytest.param(None, (_LANG, _DEFAULT_AGENT), id="none"),
            pytest.param(
                {"user_agent": ":default"}, (_LANG, _DEFAULT_AGENT), id="default"
            ),
            pytest.param(
                {"user_agent": ":curl"},
                (_LANG, ("User-Agent", "curl/8.7.1")),
                id="curl",
            ),
            pytest.param(
                {"user_agent": "Foo/1.0"},
                (_LANG, ("User-Agent", "Foo/1.0")),
                id="string",
            ),
            pytest.param(
                {"referer": "https://a/"},
                (_LANG, _DEFAULT_AGENT, ("Referer", "https://a/")),
                id="referer",
            ),
            pytest.param(
                {"cookies": {"a": "1", "b": "2"}},
                (_LANG, _DEFAULT_AGENT, ("Cookie", "a=1;b=2")),
                id="cookies",
            ),
        ],
    )
    def test_headers_per_key(self, specs, expected) -> None:
        """Test that each `url` option becomes the header brew's curl sends."""
        assert url_headers(specs) == expected

    @pytest.mark.parametrize("agent", [":fake", ":browser"])
    def test_fake_agent_is_brews_safari(self, agent) -> None:
        """Test that `:fake` and `:browser` send brew's Safari agent."""
        _, (name, value) = url_headers({"user_agent": agent})
        assert (name, "Safari/" in value) == ("User-Agent", True)


class TestCurlAgent:
    """Tests for curl's own User-Agent, which `:curl` and the default agent send."""

    def test_curl_agent_from_version(self, monkeypatch) -> None:
        """Test that curl's agent is the `curl --version` text before ` (`."""
        out = "curl 8.7.1 (x86_64-apple-darwin24.0) libcurl/8.7.1\nRelease-Date: x\n"
        monkeypatch.setattr(
            cask.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out),
        )
        assert cask._curl_agent.__wrapped__() == "curl/8.7.1"

    def test_curl_agent_without_curl(self, monkeypatch) -> None:
        """Test that the agent falls back to `curl` when curl can't run."""

        def missing(*args, **kwargs) -> subprocess.CompletedProcess:
            raise FileNotFoundError("curl")

        monkeypatch.setattr(cask.subprocess, "run", missing)
        assert cask._curl_agent.__wrapped__() == "curl"


class TestReadCaskOpts:
    """Tests for read_cask_opts."""

    def test_reads_what_brew_reads(self, monkeypatch) -> None:
        """Test dirs (expanded, hyphens to underscores), languages and switches."""
        monkeypatch.setenv("HOME", "/Users/me")
        opts = read_cask_opts(
            "--appdir=~/Applications '--input-methoddir=/Library/Input Methods' "
            "--no-binaries --binaries --no-binaries --require-sha --language=de,fr "
            "--unknown"
        )

        assert opts.dirs == {
            "appdir": Path("/Users/me/Applications"),
            "input_methoddir": Path("/Library/Input Methods"),
        }
        assert (opts.binaries, opts.require_sha) == (False, True)
        assert opts.languages == ("de", "fr")

    def test_empty_means_brew_defaults(self) -> None:
        """Test that an unset variable changes nothing."""
        opts = read_cask_opts("")

        assert (opts.dirs, opts.binaries, opts.require_sha) == ({}, True, False)


def _variant(*languages: str, default: bool = False, **overrides) -> dict:
    """A feed `language_variations` entry."""
    base = {"languages": list(languages), "default": default, "value": languages[0]}

    return base | overrides


class TestLanguages:
    """Tests for choosing a cask's language variant as brew does."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("en", ("en", None, None)),
            ("en-GB", ("en", None, "GB")),
            ("zh-Hans-CN", ("zh", "Hans", "CN")),
            ("CN", (None, None, "CN")),
            ("es-419", ("es", None, "419")),
            ("en-", None),
            ("enGB", None),
            ("ja-JP-mac", None),
            ("", None),
        ],
    )
    def test_parse_locale(self, text, expected) -> None:
        """Test brew's `Locale.try_parse` rules."""
        assert parse_locale(text) == expected

    VARIANTS = (
        _variant("en", default=True),
        _variant("en-GB"),
        _variant("de"),
        _variant("zh", "CN"),
    )

    @pytest.mark.parametrize(
        ("languages", "value"),
        [
            pytest.param(["en-GB"], "en-GB", id="exact_beats_include"),
            pytest.param(["de-AT"], "de", id="region_falls_back"),
            pytest.param(["fr", "de"], "de", id="next_preference"),
            pytest.param(["zh-Hans-CN"], "zh", id="any_locale_in_group"),
            pytest.param(["bad!", "fr"], "en", id="default"),
        ],
    )
    def test_pick_language(self, languages, value) -> None:
        """Test exact matches first, then included locales, then the default."""
        assert (pick_language(self.VARIANTS, languages) or {}).get("value") == value

    def test_no_default_keeps_the_row(self) -> None:
        """Test that with no match and no default the top-level row is used."""
        row = cask_row(url="https://a/en.dmg", language_variations=[_variant("de")])

        assert localise(row, ["fr"]) == cask_row(url="https://a/en.dmg")

    def test_localise_applies_the_variant(self) -> None:
        """Test that the variant's fields replace the row's, so the cask is native."""
        row = cask_row(
            url="https://a/en.dmg",
            sha256="a" * 64,
            artifacts=[{"app": ["Foo.app"]}],
            language_variations=[
                _variant("en", default=True),
                _variant("de", url="https://a/de.dmg", sha256="b" * 64),
            ],
        )

        localised = localise(row, ["de-DE"])

        assert (localised.url, localised.sha256) == ("https://a/de.dmg", "b" * 64)
        assert classify(localised, SEQUOIA) == ("native", "")

    def test_unknown_override_goes_to_brew(self) -> None:
        """Test that a variant overriding a field brewery doesn't store stays brew's."""
        row = cask_row(
            artifacts=[{"app": ["Foo.app"]}],
            language_variations=[_variant("de", url_specs={"referer": "x"})],
        )

        assert classify(localise(row, ["de"]), SEQUOIA) == (
            "brew",
            "`language` variant",
        )


class TestClassifyUninstall:
    """Tests for routing an installed cask's uninstall from its receipt."""

    ARTIFACTS = (
        {"uninstall": [{"quit": "org.example.foo", "delete": "/tmp/foo"}]},
        {"app": ["Foo.app"]},
        {"zap": [{"trash": "~/foo", "pkgutil": "org.example.foo"}]},
    )

    @pytest.mark.parametrize(
        ("receipt", "zap", "reason"),
        [
            pytest.param({"uninstall_artifacts": [*ARTIFACTS]}, False, "", id="native"),
            pytest.param(
                {"uninstall_artifacts": [*ARTIFACTS]},
                True,
                "`zap pkgutil`",
                id="zap_brew",
            ),
            pytest.param(
                {"uninstall_artifacts": [{"pkg": ["Foo.pkg"]}]},
                False,
                "`pkg` artifact",
                id="pkg",
            ),
            pytest.param(None, False, "no uninstall receipt", id="no_receipt"),
            pytest.param({}, False, "no uninstall receipt", id="no_artifacts"),
            pytest.param(
                {"uninstall_artifacts": []}, False, "", id="nothing_to_remove"
            ),
            pytest.param(
                {"uninstall_artifacts": [{"uninstall": ["quit"]}]},
                False,
                "unrecognised `uninstall` directive",
                id="directive_not_a_dict",
            ),
            pytest.param(
                {"uninstall_artifacts": [{"uninstall": {"quit": "org.example.foo"}}]},
                False,
                "unrecognised `uninstall` directive",
                id="directives_not_a_list",
            ),
        ],
    )
    def test_reasons(self, receipt, zap, reason) -> None:
        """Test that the receipt decides, and `zap` directives count only for --zap."""
        assert classify_uninstall(receipt, SEQUOIA, zap=zap) == reason
