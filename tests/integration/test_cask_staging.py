"""Integration tests for staging casks into a brew-shaped Caskroom and installing them."""

from __future__ import annotations

import getpass
import gzip
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest
from _rows import cask_row
from _tar import make_tar

from brewery.core.errors import CellarError, ExtractionError, LinkError
from brewery.core.fs_state import scan_installed
from brewery.core.models import PackageKind
from brewery.providers import cask_directives, caskroom
from brewery.providers.caskroom import (
    ALT_NAMES,
    QUARANTINE,
    _installed_specs,
    install_artifacts,
    stage,
    uninstall_cask,
    upgrade_cask,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="native casks are macOS-only"
)

ARTIFACTS = [
    {"uninstall": [{"quit": "org.example.foo"}]},
    {"app": ["Foo.app"], "target": "/Applications/Foo.app"},
]


def _quarantine(path) -> str:
    """Read a path's quarantine xattr, without following a symlink.

    Args:
        path: The path to read.

    Returns:
        The xattr's value, or an empty string if it has none.
    """
    proc = subprocess.run(
        ["xattr", "-s", "-p", QUARANTINE, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def _app(root) -> None:
    """Lay out a minimal `Foo.app` under `root`, with an internal symlink.

    Args:
        root: The directory to build the bundle in.
    """
    macos = root / "Foo.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "foo").write_text("#!/bin/sh\n")
    (root / "Foo.app" / "Contents" / "Current").symlink_to("MacOS")


def _row(url: str = "https://example.org/Foo-1.0.zip"):
    """A cask row for the synthetic `foo` cask.

    Args:
        url: The download URL, whose filename names a bare download.

    Returns:
        The CaskRow.
    """
    return cask_row("foo", version="1.0", url=url, artifacts=ARTIFACTS)


@pytest.fixture
def zipped(tmp_path):
    """A zip of `Foo.app`, made by ditto as vendors' zips are.

    Args:
        tmp_path: The pytest tmp_path fixture.

    Returns:
        The zip's path.
    """
    src = tmp_path / "src"
    _app(src)
    archive = tmp_path / "download"
    subprocess.run(
        ["ditto", "-c", "-k", "--keepParent", str(src / "Foo.app"), str(archive)],
        check=True,
    )
    return archive


class TestStage:
    """Tests for stage."""

    def test_zip_stages_with_symlinks_and_quarantine(self, brew, zipped) -> None:
        """Test that a zip unpacks into the version dir, quarantined throughout."""
        staged = stage(_row(), zipped, brew.caskroom)

        assert staged == brew.caskroom / "foo" / "1.0"
        assert (staged / "Foo.app" / "Contents" / "MacOS" / "foo").is_file()
        assert (staged / "Foo.app" / "Contents" / "Current").is_symlink()
        assert _quarantine(zipped) == ""  # The cache is left untouched
        assert _quarantine(staged / "Foo.app").startswith("0181;")
        assert _quarantine(staged / "Foo.app" / "Contents" / "MacOS" / "foo")

    def test_metadata_matches_brews_layout(self, brew, zipped) -> None:
        """Test the caskfile and receipt brew reads, and that brewery lists it."""
        stage(_row(), zipped, brew.caskroom)
        metadata = brew.caskroom / "foo" / ".metadata"

        (caskfile,) = metadata.glob("1.0/*/Casks/foo.json")
        assert json.loads(caskfile.read_text()) == {}

        receipt = json.loads((metadata / "INSTALL_RECEIPT.json").read_text())
        assert receipt["source"]["version"] == "1.0"
        assert receipt["uninstall_artifacts"] == [
            {"uninstall": [{"quit": "org.example.foo"}]},
            {"app": ["Foo.app"]},
        ]

        casks = [r for r in scan_installed(brew.env) if r.kind is PackageKind.CASK]
        assert [(r.name, r.version) for r in casks] == [("foo", "1.0")]

    def test_caskfile_keeps_only_path_for_upgrades(self, brew, zipped) -> None:
        """Test that the installed `only_path` is read back, not the catalog's."""
        token_dir = brew.caskroom / "foo"
        assert _installed_specs(token_dir, "1.0") is None

        row = cask_row(
            "foo",
            version="1.0",
            url="https://example.org/Foo-1.0.zip",
            url_specs={"branch": "main", "only_path": "ofl/foo"},
            artifacts=ARTIFACTS,
        )
        stage(row, zipped, brew.caskroom)

        assert _installed_specs(token_dir, "1.0") == {"only_path": "ofl/foo"}

    def test_tar_stages(self, brew, tmp_path) -> None:
        """Test that a gzipped tarball unpacks through the bottle extractor."""
        archive = tmp_path / "download"
        archive.write_bytes(
            gzip.compress(
                make_tar(
                    [
                        ("dir", "Foo.app", 0o755),
                        ("file", "Foo.app/Info.plist", b"<plist/>", 0o644),
                    ]
                )
            )
        )

        staged = stage(_row(), archive, brew.caskroom)

        assert (staged / "Foo.app" / "Info.plist").read_bytes() == b"<plist/>"

    def test_dmg_stages_without_volume_furniture(self, brew, tmp_path) -> None:
        """Test that a dmg's contents are copied out and the image detached."""
        src = tmp_path / "src"
        _app(src)
        (src / ".DS_Store").write_bytes(b"")
        (src / "Applications").symlink_to("/Applications")
        dmg = tmp_path / "Foo.dmg"
        subprocess.run(
            ["hdiutil", "create", "-quiet", "-fs", "HFS+", "-srcfolder", str(src),
             "-format", "UDZO", str(dmg)],
            check=True,
        )  # fmt: skip
        download = tmp_path / "download"
        dmg.rename(download)

        staged = stage(_row(), download, brew.caskroom)

        assert sorted(p.name for p in staged.iterdir()) == ["Foo.app"]
        assert (staged / "Foo.app" / "Contents" / "Current").is_symlink()
        info = subprocess.run(
            ["hdiutil", "info"], capture_output=True, text=True, check=True
        )
        assert str(download) not in info.stdout

    def test_only_read_only_paths_are_made_writable(self, brew, tmp_path) -> None:
        """Test that a read-only file gains `S_IWUSR`, and a writable one keeps its mode."""
        src = tmp_path / "src"
        _app(src)
        resources = src / "Foo.app" / "Contents" / "Resources"
        resources.mkdir()
        (resources / "locked").write_text("")
        (resources / "locked").chmod(0o444)
        (resources / "open").write_text("")
        (resources / "open").chmod(0o640)
        archive = tmp_path / "download"
        subprocess.run(
            ["ditto", "-c", "-k", "--keepParent", str(src / "Foo.app"), str(archive)],
            check=True,
        )

        staged = stage(_row(), archive, brew.caskroom) / "Foo.app" / "Contents"

        assert (staged / "Resources" / "locked").stat().st_mode & 0o777 == 0o644
        assert (staged / "Resources" / "open").stat().st_mode & 0o777 == 0o640

    def test_bare_file_stages_under_its_url_name(self, brew, tmp_path) -> None:
        """Test that a naked download keeps the filename its artifact names."""
        download = tmp_path / "download"
        download.write_bytes(b"\x00\x01\x00\x00font")

        staged = stage(
            _row("https://example.org/fonts/Foo%5Bwght%5D.ttf"), download, brew.caskroom
        )

        assert (staged / "Foo[wght].ttf").read_bytes() == b"\x00\x01\x00\x00font"

    def test_flat_pkg_stages_whole(self, brew, tmp_path) -> None:
        """Test that a xar named `.pkg` is staged as is, as brew's `Pkg` strategy does."""
        download = tmp_path / "download"
        download.write_bytes(b"xar!\x00\x1c" + b"\x00" * 32)

        staged = stage(_row("https://example.org/Foo-1.0.pkg"), download, brew.caskroom)

        assert (staged / "Foo-1.0.pkg").read_bytes() == download.read_bytes()

    def test_bare_pkg_takes_its_stanzas_name(self, brew, tmp_path) -> None:
        """Test that a pkg from a URL without its name is staged as its stanza says."""
        download = tmp_path / "download"
        download.write_bytes(b"xar!\x00\x1c" + b"\x00" * 32)
        row = cask_row(
            "foo",
            version="1.0",
            url="https://example.org/download.php?id=1",
            artifacts=[{"pkg": ["Foo Installer.pkg"]}],
        )

        staged = stage(row, download, brew.caskroom)

        assert (staged / "Foo Installer.pkg").read_bytes() == download.read_bytes()

    def test_unsupported_archive_raises_and_stages_nothing(
        self, brew, tmp_path
    ) -> None:
        """Test that a 7z goes back to brew, leaving no version behind."""
        download = tmp_path / "download"
        download.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)

        with pytest.raises(ExtractionError, match="7z"):
            stage(_row(), download, brew.caskroom)

        assert list((brew.caskroom / "foo").iterdir()) == []

    def test_already_staged_version_is_kept(self, brew, zipped) -> None:
        """Test that restaging a version raises rather than clobbering it."""
        staged = stage(_row(), zipped, brew.caskroom)

        with pytest.raises(ExtractionError, match="already staged"):
            stage(_row(), zipped, brew.caskroom)

        assert (staged / "Foo.app").is_dir()
        assert sorted(p.name for p in (brew.caskroom / "foo").iterdir()) == [
            ".metadata",
            "1.0",
        ]


# In brew's install order, with the feed's sibling `target` hints, one of them wrong
FULL = [
    {"uninstall": [{"quit": "org.example.foo"}]},
    {"app": ["Foo.app", {"target": "Foo Renamed.app"}], "target": "/Applications/x"},
    {"font": ["Foo.ttf"], "target": "/$HOME/Library/Fonts/Foo.ttf"},
    {"qlplugin": ["Foo.qlgenerator"]},
    {
        "artifact": ["share", {"target": "$HOMEBREW_PREFIX/share/foo"}],
        "target": "$HOMEBREW_PREFIX/share/foo",
    },
    {
        "binary": ["$APPDIR/Foo Renamed.app/Contents/MacOS/foo", {"target": "foo"}],
        "target": "$HOMEBREW_PREFIX/bin/$APPDIR/foo",
    },
    {"binary": ["bin/foo-cli"]},
    {"manpage": ["$APPDIR/Foo Renamed.app/Contents/Resources/foo.1.gz"]},
    {"bash_completion": ["completions/foo.bash"]},
    {"zsh_completion": ["completions/foo.zsh"]},
    {"fish_completion": ["completions/foo"]},
]


class TestInstallArtifacts:
    """Tests for install_artifacts."""

    @pytest.fixture
    def staged(self, brew, tmp_path):
        """A staged cask carrying every simple artifact type.

        Args:
            brew: The hermetic Homebrew layout.
            tmp_path: The pytest tmp_path fixture.

        Returns:
            The staged version dir.
        """
        src = tmp_path / "src"
        _app(src)
        (src / "Foo.app" / "Contents" / "Resources").mkdir()
        (src / "Foo.app" / "Contents" / "Resources" / "foo.1.gz").write_bytes(b"")
        (src / "Foo.ttf").write_bytes(b"font")
        (src / "Foo.qlgenerator").mkdir()
        (src / "share").mkdir()
        (src / "bin").mkdir()
        (src / "bin" / "foo-cli").write_text("#!/bin/sh\n")
        (src / "completions").mkdir()
        for name in ("foo.bash", "foo.zsh", "foo"):
            (src / "completions" / name).write_text("")

        archive = tmp_path / "download"
        subprocess.run(["ditto", "-c", "-k", str(src), str(archive)], check=True)
        row = cask_row("foo", version="1.0", url="https://e.org/f.zip", artifacts=FULL)
        return stage(row, archive, brew.caskroom)

    @pytest.fixture
    def dirs(self, tmp_path):
        """Temporary stand-ins for /Applications and ~/Library.

        Args:
            tmp_path: The pytest tmp_path fixture.

        Returns:
            The dirs mapping.
        """
        return {k: tmp_path / k for k in ("appdir", "fontdir", "qlplugindir")}

    def test_installs_every_simple_artifact(self, brew, staged, dirs) -> None:
        """Test moves, links, completion renames and the config brew reads back."""
        row = cask_row("foo", version="1.0", artifacts=FULL)

        install_artifacts(row, staged, brew.env, dirs=dirs)

        app = dirs["appdir"] / "Foo Renamed.app"
        assert (app / "Contents" / "MacOS" / "foo").is_file()
        assert os.readlink(staged / "Foo.app") == str(app)
        assert (dirs["fontdir"] / "Foo.ttf").read_bytes() == b"font"
        assert (dirs["qlplugindir"] / "Foo.qlgenerator").is_dir()
        assert (brew.prefix / "share" / "foo").is_dir()
        assert (staged / "share").is_symlink()

        links = {
            "bin/foo": app / "Contents" / "MacOS" / "foo",
            "bin/foo-cli": staged / "bin" / "foo-cli",
            "share/man/man1/foo.1.gz": app / "Contents" / "Resources" / "foo.1.gz",
            "etc/bash_completion.d/foo": staged / "completions" / "foo.bash",
            "share/zsh/site-functions/_foo": staged / "completions" / "foo.zsh",
            "share/fish/vendor_completions.d/foo.fish": staged / "completions" / "foo",
        }
        for rel, source in links.items():
            assert os.readlink(brew.prefix / rel) == str(source), rel
        assert os.access(staged / "bin" / "foo-cli", os.X_OK)
        alt = subprocess.run(
            ["xattr", "-p", ALT_NAMES, str(app)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert alt.stdout.strip() == '("Foo.app")'

        config = json.loads((staged.parent / ".metadata" / "config.json").read_text())
        assert config["default"]["appdir"] == "/Applications"
        assert config["env"]["appdir"] == str(dirs["appdir"])

    def test_no_binaries_skips_binary_links(self, brew, staged, dirs) -> None:
        """Test that --no-binaries leaves bin/ alone and still links the rest."""
        row = cask_row("foo", version="1.0", artifacts=FULL)

        install_artifacts(row, staged, brew.env, dirs=dirs, binaries=False)

        assert not (brew.prefix / "bin").exists()
        assert (brew.prefix / "share" / "man" / "man1" / "foo.1.gz").is_symlink()

    def test_command_wrappers_are_written_and_linked(self, brew, staged, dirs) -> None:
        """Test both wrapper kinds: an env script with quoted args, and `content`."""
        exe = {"executable": "/bin/sh", "args": ["-c", 'echo "$FOO $0 $1"', "a b"]}
        row = cask_row(
            "foo",
            version="1.0",
            artifacts=[
                {"command_wrapper": ["foo-run", {**exe, "env": {"FOO": "x"}}]},
                {"command_wrapper": ["foo-raw", {"content": "$HOMEBREW_PREFIX\n"}]},
            ],
        )

        install_artifacts(row, staged, brew.env, dirs=dirs)

        run = brew.prefix / "bin" / "foo-run"
        assert os.readlink(run) == str(
            staged / ".homebrew-command-wrappers" / "foo-run"
        )
        out = subprocess.run([run, "c"], capture_output=True, text=True, check=True)
        assert out.stdout == "x a b c\n"
        assert (brew.prefix / "bin" / "foo-raw").read_text() == f"{brew.prefix}\n"

    @pytest.mark.parametrize("args", [["foo-run"], ["foo-run", {}]])
    def test_malformed_command_wrapper_raises(self, brew, staged, dirs, args) -> None:
        """Test that a wrapper with no content or executable is a CellarError."""
        row = cask_row("foo", version="1.0", artifacts=[{"command_wrapper": args}])

        with pytest.raises(CellarError, match="no content or executable"):
            install_artifacts(row, staged, brew.env, dirs=dirs)

        assert not (brew.prefix / "bin").exists()

    def test_pkg_runs_installer_with_choices(
        self, brew, staged, dirs, monkeypatch
    ) -> None:
        """Test `installer`'s args, env and choices, and that a missing pkg raises."""
        (staged / "Foo.pkg").write_bytes(b"xar!")
        calls = []

        def run_sudo(*args: str, env: dict[str, str]) -> None:
            choices = Path(args[args.index("-applyChoiceChangesXML") + 1])
            calls.append((args[:5], env, plistlib.loads(choices.read_bytes())))

        monkeypatch.setattr(caskroom, "run_sudo", run_sudo)
        choices = [
            {
                "choiceIdentifier": "x",
                "choiceAttribute": "selected",
                "attributeSetting": 0,
            }
        ]
        row = cask_row(
            "foo",
            version="1.0",
            artifacts=[{"pkg": ["Foo.pkg", {"choices": choices}]}, {"pkg": ["No.pkg"]}],
        )

        with pytest.raises(CellarError, match="No.pkg is not there"):
            install_artifacts(row, staged, brew.env, dirs=dirs)

        user = getpass.getuser()
        assert calls == [
            (
                (
                    "/usr/sbin/installer",
                    "-pkg",
                    str(staged / "Foo.pkg"),
                    "-target",
                    "/",
                ),
                {"LOGNAME": user, "USER": user, "USERNAME": user},
                choices,
            )
        ]

    def test_conflict_puts_everything_back(self, brew, staged, dirs) -> None:
        """Test that a taken target raises and restores what was already moved."""
        taken = brew.prefix / "bin" / "foo-cli"
        taken.parent.mkdir()
        taken.write_text("mine")
        row = cask_row("foo", version="1.0", artifacts=FULL)

        with pytest.raises(LinkError, match="foo-cli"):
            install_artifacts(row, staged, brew.env, dirs=dirs)

        assert (staged / "Foo.app").is_dir()
        assert not (staged / "Foo.app").is_symlink()
        assert list(dirs["appdir"].iterdir()) == []
        assert not (brew.prefix / "bin" / "foo").is_symlink()
        assert taken.read_text() == "mine"
        assert not (staged.parent / ".metadata" / "config.json").exists()

    def test_formula_owned_link_is_kept(self, brew, staged, dirs) -> None:
        """Test that a binary a formula already links is skipped, as brew does."""
        keg_bin = brew.cellar / "foo" / "1.0" / "bin"
        keg_bin.mkdir(parents=True)
        (keg_bin / "foo-cli").write_text("")
        (brew.prefix / "bin").mkdir()
        (brew.prefix / "bin" / "foo-cli").symlink_to(keg_bin / "foo-cli")
        row = cask_row("foo", version="1.0", artifacts=FULL)

        install_artifacts(row, staged, brew.env, dirs=dirs)

        assert os.readlink(brew.prefix / "bin" / "foo-cli") == str(keg_bin / "foo-cli")

    def test_rollback_keeps_a_link_it_found(self, brew, staged, dirs) -> None:
        """Test that a link already in place survives a later conflict's rollback."""
        found = brew.prefix / "bin" / "foo-cli"
        found.parent.mkdir()
        found.symlink_to(staged / "bin" / "foo-cli")
        taken = brew.prefix / "etc" / "bash_completion.d" / "foo"
        taken.parent.mkdir(parents=True)
        taken.write_text("mine")
        row = cask_row("foo", version="1.0", artifacts=FULL)

        with pytest.raises(LinkError, match="bash_completion"):
            install_artifacts(row, staged, brew.env, dirs=dirs)

        assert os.readlink(found) == str(staged / "bin" / "foo-cli")

    def test_flight_steps_run_around_the_artifacts(self, brew, staged, dirs) -> None:
        """Test that preflight sees the staged app, and postflight the moved one."""
        mark = {"base": "temp", "path": "seen"}
        row = cask_row(
            "foo",
            version="1.0",
            artifacts=[
                {
                    "preflight_steps": [
                        {
                            "steps": [
                                {"type": "touch", "path": mark},
                                {
                                    "type": "touch",
                                    "path": {"base": "staged_path", "path": "pre"},
                                    "guards": [
                                        {
                                            "condition": "if_exists",
                                            "path": "{{staged_path}}/Foo.app",
                                        }
                                    ],
                                },
                            ]
                        }
                    ]
                },
                {"app": ["Foo.app"]},
                {
                    "postflight_steps": [
                        {
                            "steps": [
                                {
                                    "type": "set_permissions",
                                    "paths": [{"base": "appdir", "path": "Foo.app"}],
                                    "permissions": "go-w",
                                },
                                {
                                    "type": "write",
                                    "path": {"base": "appdir", "path": "post"},
                                    "content": "{{temp}}",
                                },
                            ]
                        }
                    ]
                },
            ],
        )

        install_artifacts(row, staged, brew.env, dirs=dirs)

        assert (staged / "pre").exists()
        temp = Path((dirs["appdir"] / "post").read_text())
        assert temp.name.startswith("cask-foo-")
        assert not temp.exists()  # Removed once the install succeeds
        assert not (dirs["appdir"] / "Foo.app").stat().st_mode & 0o022


class TestUpgradeCask:
    """Tests for upgrade_cask."""

    def test_failed_upgrade_restores_old_only_path(self, brew, tmp_path) -> None:
        """Test that a rollback puts the old font back under the old `only_path`."""

        def font_row(version: str, only_path: str, artifacts: list):
            return cask_row(
                "foo",
                version=version,
                url="https://github.com/example/fonts",
                url_specs={"only_path": only_path},
                artifacts=artifacts,
            )

        old_font, new_font, cli = (tmp_path / n for n in ("old", "new", "cli"))
        old_font.write_bytes(b"old")
        new_font.write_bytes(b"new")
        cli.write_text("#!/bin/sh\n")
        dirs = {"fontdir": tmp_path / "fonts"}
        font = {"font": ["Foo.ttf"]}

        old = font_row("1.0", "a", [font])
        staged = stage(old, {"a/Foo.ttf": old_font}, brew.caskroom)
        install_artifacts(old, staged, brew.env, dirs=dirs)

        taken = brew.prefix / "bin" / "foo-cli"
        taken.parent.mkdir()
        taken.write_text("mine")
        new = font_row("2.0", "b", [font, {"binary": ["foo-cli"]}])
        download = {"b/Foo.ttf": new_font, "b/foo-cli": cli}

        with pytest.raises(LinkError, match="foo-cli"):
            upgrade_cask(new, download, brew.env, dirs=dirs)

        target = dirs["fontdir"] / "Foo.ttf"
        assert target.read_bytes() == b"old"
        assert os.readlink(staged / "a" / "Foo.ttf") == str(target)
        assert not (staged / "b").exists()
        assert not (brew.caskroom / "foo" / "2.0").exists()

    def test_failed_pkg_upgrade_reinstalls_the_old_pkg(
        self, brew, tmp_path, monkeypatch
    ) -> None:
        """Test that a rollback reinstalls the old pkg its `pkgutil` removed.

        brew's receipt has no `pkg` stanza, so the one staged pkg is used, with
        the new version's choices.
        """
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(caskroom, "run_sudo", lambda *a, **_: calls.append(a))
        monkeypatch.setattr(
            cask_directives,
            "_run",
            lambda *a: subprocess.CompletedProcess(a, 1, "", ""),
        )
        pkgutil = {"uninstall": [{"pkgutil": "org.example.foo"}]}
        old_pkg = tmp_path / "old"
        old_pkg.write_bytes(b"xar!old")
        old = cask_row(
            "foo",
            version="1.0",
            url="https://e.org/Foo-1.0.pkg",
            artifacts=[{"pkg": ["Foo-1.0.pkg"]}, pkgutil],
        )
        staged = stage(old, old_pkg, brew.caskroom)
        install_artifacts(old, staged, brew.env)

        receipt = brew.caskroom / "foo" / ".metadata" / "INSTALL_RECEIPT.json"
        data = json.loads(receipt.read_text())
        receipt.write_text(json.dumps({**data, "uninstall_artifacts": [pkgutil]}))
        calls.clear()

        bad = tmp_path / "new"
        bad.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)
        choices = {"pkg": ["Foo-2.0.pkg", {"choices": [{"choiceIdentifier": "x"}]}]}
        new = cask_row(
            "foo",
            version="2.0",
            url="https://e.org/Foo-2.0.pkg",
            artifacts=[choices, pkgutil],
        )

        with pytest.raises(ExtractionError):
            upgrade_cask(new, bad, brew.env)

        assert [c[:3] for c in calls] == [
            ("/usr/sbin/installer", "-pkg", str(staged / "Foo-1.0.pkg"))
        ]
        assert "-applyChoiceChangesXML" in calls[0]
        assert json.loads(receipt.read_text())["uninstall_artifacts"] == [pkgutil]


def _scripted(version: str):
    """A cask installed and removed by scripts, through path placeholders.

    Args:
        version: The cask version.

    Returns:
        The CaskRow.
    """
    marker = "$HOMEBREW_PREFIX/marker"
    return cask_row(
        "foo",
        version=version,
        url="https://e.org/foo",
        artifacts=[
            {"installer": [{"script": {"executable": "setup.sh", "args": [marker]}}]},
            {
                "uninstall": [
                    {"script": {"executable": "undo.sh", "args": [marker]}},
                    {"delete": "$HOMEBREW_PREFIX/extra"},
                ]
            },
        ],
    )


class TestInstallerScripts:
    """Tests for `installer` and the uninstall scripts."""

    @pytest.fixture
    def scripts(self, tmp_path, monkeypatch) -> dict[str, Path]:
        """Non-executable install and uninstall scripts, as a download.

        Args:
            tmp_path: The pytest tmp_path fixture.
            monkeypatch: The pytest monkeypatch fixture, to skip brew's 1s pause.

        Returns:
            The download mapping.
        """
        monkeypatch.setattr(cask_directives.time, "sleep", lambda _: None)
        setup, undo = tmp_path / "setup", tmp_path / "undo"
        setup.write_text('#!/bin/sh\necho ran >> "$1"\n')
        undo.write_text('#!/bin/sh\nrm "$1"\n')

        return {"setup.sh": setup, "undo.sh": undo}

    def test_script_installs_and_uninstall_script_removes(self, brew, scripts) -> None:
        """Test both scripts run, and placeholders are filled in for `delete` too."""
        row = _scripted("1.0")
        staged = stage(row, scripts, brew.caskroom)

        assert install_artifacts(row, staged, brew.env) == ""

        marker = brew.prefix / "marker"
        assert marker.read_text() == "ran\n"

        (brew.prefix / "extra").write_text("")
        uninstall_cask("foo", brew.env)

        assert not marker.exists()
        assert not (brew.prefix / "extra").exists()

    def test_manual_installer_is_left_to_the_user(self, brew, tmp_path) -> None:
        """Test that a manual installer is staged and how to open it returned."""
        (tmp_path / "x").write_text("")
        row = cask_row(
            "foo",
            version="1.0",
            url="https://e.org/foo",
            artifacts=[{"installer": [{"manual": "Setup.app"}]}],
        )
        staged = stage(row, {"Setup.app/x": tmp_path / "x"}, brew.caskroom)

        note = install_artifacts(row, staged, brew.env)

        assert note.startswith("only provides a manual installer.")
        assert note.endswith(f"\n  open {staged / 'Setup.app'}")

    def test_failed_upgrade_reruns_the_old_installer(
        self, brew, tmp_path, scripts
    ) -> None:
        """Test that a rollback reruns the installer the uninstall script undid.

        brew's receipt has no `installer` stanza, so the new version's is used,
        its executable being staged in the old version.
        """
        old = _scripted("1.0")
        install_artifacts(old, stage(old, scripts, brew.caskroom), brew.env)
        receipt = brew.caskroom / "foo" / ".metadata" / "INSTALL_RECEIPT.json"
        data = json.loads(receipt.read_text())
        uninstall = [a for a in old.artifacts if "uninstall" in a]
        receipt.write_text(json.dumps({**data, "uninstall_artifacts": uninstall}))

        bad = tmp_path / "new"
        bad.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)

        with pytest.raises(ExtractionError):
            upgrade_cask(_scripted("2.0"), bad, brew.env)

        assert (brew.prefix / "marker").read_text() == "ran\n"
