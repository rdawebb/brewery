"""Unit tests for routing casks to the native installer or to brew."""

from __future__ import annotations

from pathlib import Path

import pytest
from _rows import cask_row

from brewery.core.host import Platform
from brewery.providers.cask import classify, read_cask_opts

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
                {"container": {"type": "naked"}}, "`container` unpacking", id="naked"
            ),
            pytest.param(
                {"url_specs": {"branch": "main", "only_path": "fonts"}},
                "`url branch:`",
                id="git",
            ),
            pytest.param(
                {"url_specs": {"user_agent": ":fake"}}, "`url user_agent:`", id="header"
            ),
            pytest.param(
                {"url_specs": {"using": "post", "data": {"a": "b"}}},
                "`url data:`",
                id="post",
            ),
            pytest.param(
                {"language_variations": [{"languages": ["de"], "default": False}]},
                "`language` variants",
                id="languages",
            ),
        ],
    )
    def test_download_brew_alone_can_fetch(self, fields, reason) -> None:
        """Test that a download only brew can fetch or unpack goes to brew up front."""
        row = cask_row(artifacts=[{"app": ["Foo.app"]}], **fields)
        assert classify(row, SEQUOIA) == ("brew", reason)

    def test_off_macos_goes_to_brew(self) -> None:
        """Test that native casks are macOS-only."""
        linux = Platform(arch="amd64", os="linux")
        assert classify(cask_row(artifacts=[{"font": ["a.ttf"]}]), linux)[0] == "brew"


class TestReadCaskOpts:
    """Tests for read_cask_opts."""

    def test_reads_what_brew_reads(self, monkeypatch) -> None:
        """Test dirs (expanded, hyphens to underscores) and switches.

        `--language` isn't a dir: only `languages` casks read it, and those go to brew.
        """
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

    def test_empty_means_brew_defaults(self) -> None:
        """Test that an unset variable changes nothing."""
        opts = read_cask_opts("")

        assert (opts.dirs, opts.binaries, opts.require_sha) == ({}, True, False)
