"""Stage a cask into the Caskroom and install its artifacts.

macOS-only, like native casks themselves: `classify` sends every other host to brew.
"""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg cask installing logic.

from __future__ import annotations

import errno
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import orjson

from brewery.core.catalog import CaskRow
from brewery.core.config import BreweryENV
from brewery.core.errors import CellarError, ExtractionError, LinkError
from brewery.core.logging import BreweryLogger, get_logger
from brewery.providers.cask_directives import (
    remove,
    reopen,
    run_directives,
    undeletable,
)
from brewery.providers.extractor import extract_tar
from brewery.providers.receipt import RECEIPT_NAME, current_arch, dumps_bytes

log: BreweryLogger = get_logger(name=__name__)

QUARANTINE = "com.apple.quarantine"
ALT_NAMES = "com.apple.metadata:kMDItemAlternateNames"
USER_APPROVED = 0x0040  # The quarantine flag set once the user lets an app open

# brew's defaults for the dirs moved artifacts go to, keyed as its config.json keys them
DEFAULT_DIRS: Mapping[str, Path] = {
    "appdir": Path("/Applications"),
    "fontdir": Path.home() / "Library" / "Fonts",
    "qlplugindir": Path.home() / "Library" / "QuickLook",
}

# Artifacts moved out of the Caskroom, and the dir a relative target lands in
_MOVED = {
    "app": "appdir",
    "suite": "appdir",
    "font": "fontdir",
    "qlplugin": "qlplugindir",
    "artifact": None,  # brew requires an absolute target
}

# Artifacts symlinked into the prefix, and where under it
_LINKED = {
    "binary": "bin",
    "manpage": "share/man",
    "bash_completion": "etc/bash_completion.d",
    "zsh_completion": "share/zsh/site-functions",
    "fish_completion": "share/fish/vendor_completions.d",
}

_MANPAGE_SECTION = re.compile(r"\.([1-8]|n|l)(?:\.gz)?$")

# brew's `Cask::Metadata::TIMESTAMP_FORMAT`, to the millisecond
_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S.%f"

# Volume furniture brew leaves behind when copying out of a dmg
_DMG_METADATA = frozenset(
    {
        ".background",
        ".com.apple.timemachine.donotpresent",
        ".com.apple.timemachine.supported",
        ".DocumentRevisions-V100",
        ".DS_Store",
        ".fseventsd",
        ".MobileBackups",
        ".Spotlight-V100",
        ".TemporaryItems",
        ".Trashes",
        ".VolumeIcon.icns",
        ".HFS+ Private Directory Data\r",
        ".HFS+ Private Data\r",
        "Desktop DB",
        "Desktop DF",
    }
)

# Archives brewery can't open; raising falls back to brew
_UNSUPPORTED = {b"7z\xbc\xaf": "7z", b"Rar!": "rar", b"xar!": "xar"}

_TAR_MAGIC = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ", b"\x28\xb5\x2f\xfd")


def stage(
    row: CaskRow, download: Path, caskroom: Path, *, on_request: bool = True
) -> Path:
    """Unpack `download` into `Caskroom/<token>/<version>/` and record it as brew does.

    The download is quarantined, and the quarantine copied onto everything
    staged from it; brew reads the version from `.metadata/<version>/` and the
    artifacts to uninstall from `.metadata/INSTALL_RECEIPT.json`.

    Args:
        row: The cask's catalog row.
        download: The verified download, from the downloader's cache.
        caskroom: The Caskroom directory.
        on_request: False when it's installed for another cask's `depends_on`.

    Returns:
        The staged version directory.

    Raises:
        ExtractionError: If the download can't be unpacked or that version is
            already staged.
    """
    if not row.version or not row.url:
        raise ExtractionError(f"{row.token} has no version or url", archive=download)

    token_dir = caskroom / row.token
    staged = token_dir / row.version
    token_dir.mkdir(parents=True, exist_ok=True)

    stamp = f"{int(time.time()):x};brewery;"
    _run(["xattr", "-w", QUARANTINE, f"0081;{stamp}", str(download)])

    # Hidden, so `_scan_casks` never reports a half-staged version
    scratch = Path(tempfile.mkdtemp(prefix=".stage-", dir=token_dir))
    try:
        _unpack(download, scratch, name=_url_name(row.url) or row.token)
        _run(["chmod", "-R", "u+w", str(scratch)])
        # 0x0100 opts the staged app out of App Translocation
        _run(["xattr", "-r", "-s", "-w", QUARANTINE, f"0181;{stamp}", str(scratch)])

        try:
            scratch.rename(staged)

        except OSError as exc:
            raise ExtractionError(
                f"{row.token} {row.version} is already staged", dest=staged
            ) from exc

    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    try:
        _write_metadata(row, token_dir, on_request=on_request)
        if row.version == "latest":
            # brew's `save_download_sha`, which its `upgrade --greedy` compares
            with download.open("rb") as fh:
                sha = hashlib.file_digest(fh, "sha256").hexdigest()
            (token_dir / ".metadata" / "LATEST_DOWNLOAD_SHA256").write_text(sha)

    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    return staged


def unstage(token: str, version: str, caskroom: Path) -> None:
    """Remove a staged version, and the cask's dir if nothing else is in it.

    Args:
        token: The cask's token.
        version: The staged version.
        caskroom: The Caskroom directory.
    """
    token_dir = caskroom / token
    shutil.rmtree(token_dir / version, ignore_errors=True)
    shutil.rmtree(token_dir / ".metadata" / version, ignore_errors=True)
    if token_dir.is_dir() and all(p.name == ".metadata" for p in token_dir.iterdir()):
        shutil.rmtree(token_dir)


def _write_metadata(row: CaskRow, token_dir: Path, *, on_request: bool) -> None:
    """Write the installed caskfile and receipt for an API cask.

    `runtime_dependencies` stays empty: brew's autoremove reads a cask's
    `depends_on` from the cask itself, not from this receipt.

    Args:
        row: The cask's catalog row.
        token_dir: The cask's Caskroom directory.
        on_request: Whether the user asked for it, rather than a dependent cask.
    """
    version = str(row.version)
    # The `target` key is install-time only
    artifacts = [
        {k: v for k, v in stanza.items() if k != "target"}
        for stanza in row.artifacts or []
    ]

    stamp = datetime.now(UTC).strftime(_TIMESTAMP_FORMAT)[:-3]
    casks = token_dir / ".metadata" / version / stamp / "Casks"
    casks.mkdir(parents=True)
    # brew writes `{}`, and pins an empty list so it never looks the artifacts up
    (casks / f"{row.token}.json").write_bytes(
        dumps_bytes({} if artifacts else {"artifacts": []})
    )

    receipt = {
        "homebrew_version": None,
        "loaded_from_api": True,
        "loaded_from_internal_api": False,
        "uninstall_flight_blocks": False,
        "installed_as_dependency": not on_request,
        "installed_on_request": on_request,
        "time": int(time.time()),
        "runtime_dependencies": {},
        "source": {
            "path": None,
            "tap": row.tap,
            "tap_git_head": None,
            "version": version,
        },
        "arch": current_arch(),
        "uninstall_artifacts": artifacts,
        "built_on": None,
    }
    (token_dir / ".metadata" / RECEIPT_NAME).write_bytes(dumps_bytes(receipt))


def install_artifacts(
    row: CaskRow,
    staged: Path,
    env: BreweryENV,
    *,
    dirs: Mapping[str, Path] | None = None,
    binaries: bool = True,
    languages: Sequence[str] = (),
    explicit: Mapping[str, Any] | None = None,
    reuse: Collection[Path] = (),
) -> None:
    """Move and link a staged cask's artifacts into place, as brew does.

    Apps, suites, fonts, Quick Look plugins and generic artifacts are moved out,
    leaving a symlink in the version dir that brew's uninstall moves them back
    over. Binaries, manpages and completions are symlinked into the prefix. On
    failure, whatever was already installed is put back.

    Args:
        row: The cask's catalog row.
        staged: The version dir `stage` returned.
        env: The Homebrew layout.
        dirs: Overrides for `DEFAULT_DIRS`, from `HOMEBREW_CASK_OPTS`; brew
            reads both back from `.metadata/config.json` to uninstall.
        binaries: False skips `binary` artifacts, as `--no-binaries` does.
        languages: `--language` values, recorded so `brew upgrade` keeps the variant.
        explicit: The installed config's `explicit` part, which an upgrade keeps.
        reuse: Targets an upgrade emptied rather than removed, to move into.

    Raises:
        LinkError: If a target is already taken.
        CellarError: If an artifact's source is missing or can't be moved.
    """
    overrides = dict(dirs or {})
    explicit = dict(explicit or {})
    dirs = {**DEFAULT_DIRS, **overrides, **_dir_values(explicit)}
    done: list[tuple[str, Path, Path]] = []
    try:
        # The feed lists artifacts in brew's install order: apps before binaries into them
        for stanza in row.artifacts or []:
            for key, args in stanza.items():
                if key not in _MOVED and key not in _LINKED:
                    continue  # `uninstall`, `zap` and the feed's own `target` hint

                if key == "binary" and not binaries:
                    continue

                source, target = _locate(key, args, staged, env, dirs)
                if not source.exists():
                    raise CellarError(
                        f"{row.token}: {source} is not there", path=source
                    )

                if key in _MOVED:
                    _move_out(source, target, reuse=target in reuse)

                elif not _link(source, target, env.cellar, binary=key == "binary"):
                    continue

                done.append((key, source, target))

    except BaseException:
        for key, source, target in reversed(done):
            try:
                if key in _MOVED:
                    source.unlink()
                    _relocate(target, source)

                else:
                    target.unlink()

            except (OSError, CellarError) as exc:
                log.warning(f"couldn't put back {target}: {exc}")

        raise

    if any(key == "qlplugin" for key, _, _ in done):
        subprocess.run(["qlmanage", "-r"], capture_output=True, check=False)

    config = {
        "default": {k: str(v) for k, v in DEFAULT_DIRS.items()},
        "env": {k: str(v) for k, v in overrides.items()}
        | ({"languages": list(languages)} if languages else {}),
        "explicit": explicit,
    }
    (staged.parent / ".metadata" / "config.json").write_bytes(dumps_bytes(config))


def read_receipt(token_dir: Path) -> dict[str, Any] | None:
    """An installed cask's `INSTALL_RECEIPT.json`.

    Args:
        token_dir: The cask's Caskroom directory.

    Returns:
        The receipt, or None if it's missing or unreadable.
    """
    try:
        receipt = orjson.loads((token_dir / ".metadata" / RECEIPT_NAME).read_bytes())

    except (OSError, ValueError):
        return None

    return receipt if isinstance(receipt, dict) else None


def uninstall_cask(token: str, env: BreweryENV, *, zap: bool = False) -> None:
    """Remove an installed cask from its receipt.

    Runs the `uninstall` directives (and `zap`'s), removes the moved and linked
    artifacts, then the staged version; `--zap` removes the whole Caskroom dir;
    a step that fails is retried by brew.

    Args:
        token: The installed cask's token.
        env: The Homebrew layout.
        zap: Also run the `zap` directives, as `--zap` does.

    Raises:
        CellarError: If a step can't be done without root, or the cask isn't installed.
        OSError: If an artifact can't be removed.
    """
    token_dir = env.caskroom / token
    receipt = read_receipt(token_dir) or {}
    version, config = _installed(token_dir)
    dirs = _config_dirs(config)
    artifacts = receipt.get("uninstall_artifacts") or []
    keys = ("uninstall", "zap") if zap else ("uninstall",)
    directives = [d for stanza in artifacts for k in keys for d in stanza.get(k, [])]

    run_directives(directives, deferred=False)
    reload = False
    for stanza in artifacts:
        for key, args in stanza.items():
            if key not in _MOVED and key not in _LINKED:
                continue

            _source, target = _locate(key, args, token_dir / version, env, dirs)
            if key in _MOVED:
                if undeletable(target):
                    raise CellarError(f"{target} can't be removed", path=target)

                remove(target)
                reload |= key == "qlplugin"

            # Like `_link`, a formula's link is left alone
            elif target.is_symlink() and not target.resolve().is_relative_to(
                env.cellar.resolve()
            ):
                target.unlink()

    if reload:
        subprocess.run(["qlmanage", "-r"], capture_output=True, check=False)

    run_directives(directives, deferred=True)
    if zap:
        shutil.rmtree(token_dir)

    else:
        unstage(token, version, env.caskroom)


def upgrade_cask(
    row: CaskRow,
    download: Path,
    env: BreweryENV,
    *,
    dirs: Mapping[str, Path] | None = None,
    binaries: bool = True,
    languages: Sequence[str] = (),
) -> None:
    """Replace an installed cask with the version `row` describes.

    The old version's `uninstall` directives run (`signal` only if `on_upgrade`
    lists it), then its artifacts are set aside in its version dir; an app dir
    the new version also installs is emptied rather than removed, since macOS
    treats a removed app as uninstalled; if staging or installing the new version
    fails, the old version, its receipt and its artifacts are put back, and any
    apps it quit are reopened; the user's Gatekeeper approval of the old apps
    carries over to the new ones when they're signed by the same identity.

    Args:
        row: The new version's catalog row, already localised.
        download: The new version's verified download.
        env: The Homebrew layout.
        dirs: `HOMEBREW_CASK_OPTS` dir overrides; the installed config's
            `explicit` part is kept over them.
        binaries: False skips `binary` artifacts, as `--no-binaries` does.
        languages: `--language` values, recorded as on install.

    Raises:
        CellarError: If a step can't be done without root, or the cask isn't installed.
        OSError: If an artifact can't be moved.
    """
    token_dir = env.caskroom / row.token
    meta = token_dir / ".metadata"
    receipt = read_receipt(token_dir) or {}
    version, config = _installed(token_dir)
    old = receipt.get("uninstall_artifacts") or []
    old_staged = token_dir / version
    explicit = config.get("explicit") or {}
    new_dirs = {**DEFAULT_DIRS, **(dirs or {}), **_dir_values(explicit)}
    moved = [
        (key, _locate(key, args, old_staged, env, new_dirs)[1])
        for stanza in row.artifacts or []
        for key, args in stanza.items()
        if key in _MOVED
    ]
    keep = {target for _key, target in moved}
    old_apps = [
        _locate(key, args, old_staged, env, _config_dirs(config))[1]
        for stanza in old
        for key, args in stanza.items()
        if key == "app"
    ]
    new_apps = [target for key, target in moved if key == "app"]
    signers = _approved_signers(old_apps) if len(old_apps) == len(new_apps) else None
    saved = {p: p.read_bytes() for p in meta.iterdir() if p.is_file()}
    backups = [
        (p, p.with_name(f"{p.name}.upgrading")) for p in (old_staged, meta / version)
    ]
    directives = [d for stanza in old for d in stanza.get("uninstall", [])]

    closed = run_directives(directives, deferred=False, upgrade=True)
    done: list[tuple[str, Path, Path]] = []
    backed_up = False

    try:
        _set_aside(old, old_staged, env, _config_dirs(config), keep, done)
        run_directives(directives, deferred=True)
        for path, backup in backups:
            if path.exists():
                path.rename(backup)
        backed_up = True

        staged = stage(
            row,
            download,
            env.caskroom,
            on_request=receipt.get("installed_on_request") is not False,
        )
        install_artifacts(
            row,
            staged,
            env,
            dirs=dirs,
            binaries=binaries,
            languages=languages,
            explicit=explicit,
            reuse=keep,
        )

    except BaseException:
        try:
            if backed_up:
                shutil.rmtree(token_dir / str(row.version), ignore_errors=True)
                shutil.rmtree(meta / str(row.version), ignore_errors=True)

            for path, backup in backups:
                if backup.exists():
                    backup.rename(path)

            for p in meta.iterdir():
                if p.is_file() and p not in saved:
                    p.unlink()

            for p, data in saved.items():
                p.write_bytes(data)

            _put_back(done)
            reopen(closed, old_apps)

        except (OSError, CellarError) as exc:
            log.warning(f"couldn't put back {row.token} {version}: {exc}")

        raise

    for _path, backup in backups:
        shutil.rmtree(backup, ignore_errors=True)

    if signers:
        _inherit_approval(row.token, signers, new_apps)

    reopen(closed, new_apps)


def _approved_signers(apps: Sequence[Path]) -> list[str | None] | None:
    """The apps' signers, if the user has approved all of them.

    An app with no quarantine already opens without a prompt, so counts as approved.

    Args:
        apps: The installed version's apps.

    Returns:
        Each app's designated requirement, None where it isn't validly signed;
        None instead if any app is missing or unapproved.
    """
    for app in apps:
        status = _xattr("-p", QUARANTINE, str(app))
        flags = re.match(r"[0-9a-f]+", status, re.IGNORECASE)
        if not app.exists() or (
            status and not (flags and int(flags[0], 16) & USER_APPROVED)
        ):
            return None

    return [_signer(app) for app in apps]


def _inherit_approval(token: str, signers: list[str | None], apps: list[Path]) -> None:
    """Mark the new apps approved if each is signed as its old version was.

    The quarantine stays, so Gatekeeper still checks them; best-effort: a failure
    just means macOS asks again.

    Args:
        token: The cask, for warnings.
        signers: `_approved_signers` for the old apps, in the same order as `apps`.
        apps: The new version's apps.
    """
    for requirement, app in zip(signers, apps, strict=True):
        match = _signed_by(app, requirement) if requirement else None
        if not match:
            why = "signer changed" if match is False else "signer couldn't be verified"
            log.warning(f"{token}'s {why}, so macOS may prompt at its next launch")
            return

    for app in apps:
        if not (status := _xattr("-p", QUARANTINE, str(app)).strip()):
            continue  # Not quarantined, so nothing to approve

        approved = re.sub(
            r"\A[0-9a-f]+",
            lambda m: f"{int(m[0], 16) | USER_APPROVED:0{len(m[0])}x}",
            status,
            flags=re.IGNORECASE,
        )
        cmd = ["xattr", "-r", "-s", "-w", QUARANTINE, approved, str(app)]

        if subprocess.run(cmd, capture_output=True, check=False).returncode:
            log.warning(
                f"couldn't carry over {token}'s approval, so macOS may prompt at next launch"
            )


def _signer(app: Path) -> str | None:
    """An app's designated requirement, if it's validly signed.

    Args:
        app: The app.

    Returns:
        The requirement's text, or None.
    """
    verify = ["codesign", "--verify", "--deep", "--strict", "--", str(app)]
    if subprocess.run(verify, capture_output=True, check=False).returncode:
        return None

    cmd = ["codesign", "-d", "-r-", "--", str(app)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout

    return out.partition("designated => ")[2].strip() or None


def _signed_by(app: Path, requirement: str) -> bool | None:
    """Whether an app satisfies a designated requirement.

    Args:
        app: The app.
        requirement: The requirement's text.

    Returns:
        True or False, or None if it couldn't be checked.
    """
    cmd = ["codesign", "--verify", "--deep", "--strict"]
    cmd += ["-R", f"={requirement}", "--", str(app)]

    # codesign exits 3 when the app is validly signed but fails the requirement
    return {0: True, 3: False}.get(
        subprocess.run(cmd, capture_output=True, check=False).returncode
    )


def _set_aside(
    artifacts: list[dict[str, Any]],
    staged: Path,
    env: BreweryENV,
    dirs: Mapping[str, Path],
    keep: Collection[Path],
    done: list[tuple[str, Path, Path]],
) -> None:
    """Move an installed version's artifacts back into its version dir, and unlink its links.

    Args:
        artifacts: The receipt's `uninstall_artifacts`.
        staged: The installed version dir.
        env: The Homebrew layout.
        dirs: The installed config's dirs.
        keep: Targets to empty rather than move, so the new version reuses the dir.
        done: Appended with each artifact set aside, for `_put_back`.

    Raises:
        CellarError: If a target is protected, or something else is at a source.
    """
    for stanza in artifacts:
        for key, args in stanza.items():
            if key not in _MOVED and key not in _LINKED:
                continue

            source, target = _locate(key, args, staged, env, dirs)
            if key in _LINKED:
                # Like `_link`, a formula's link is left alone
                if target.is_symlink() and not target.resolve().is_relative_to(
                    env.cellar.resolve()
                ):
                    target.unlink()
                    done.append((key, source, target))
                continue

            if not os.path.lexists(target):
                continue  # Gone already, so nothing to set aside

            if undeletable(target):
                raise CellarError(f"{target} can't be removed", path=target)

            if source.is_symlink():
                source.unlink()

            elif os.path.lexists(source):
                raise CellarError(f"{source} is in the way", path=source)

            source.parent.mkdir(parents=True, exist_ok=True)
            if target in keep and target.is_dir() and not target.is_symlink():
                source.mkdir()
                done.append((key, source, target))  # First, so a partial move goes back
                _fill(target, source)

            else:
                _relocate(target, source)
                done.append((key, source, target))


def _put_back(done: list[tuple[str, Path, Path]]) -> None:
    """Undo `_set_aside`, moving and linking each artifact back to its target.

    Args:
        done: The artifacts `_set_aside` handled.
    """
    for key, source, target in reversed(done):
        if key in _LINKED:
            if not os.path.lexists(target):
                target.symlink_to(source)
            continue

        if target.is_dir() and not target.is_symlink():
            _fill(source, target)
            source.rmdir()

        else:
            _relocate(source, target)
        source.symlink_to(target)


def _fill(source: Path, target: Path) -> None:
    """Move a dir's contents into another, existing dir.

    Args:
        source: The dir to empty.
        target: The dir to move them into.
    """
    for child in list(source.iterdir()):
        _relocate(child, target / child.name)


def _installed(token_dir: Path) -> tuple[str, dict[str, Any]]:
    """An installed cask's version and `config.json`.

    Args:
        token_dir: The cask's Caskroom directory.

    Returns:
        The version, read from the newest `.metadata/<version>/<stamp>` as brew
        does, and the config, empty if it's missing or unreadable.

    Raises:
        CellarError: If the cask has no installed version.
    """
    stamps = sorted(token_dir.glob(".metadata/*/*"), key=lambda p: p.name)
    if not stamps:
        raise CellarError(f"{token_dir.name} has no installed version", path=token_dir)

    try:
        config = orjson.loads((token_dir / ".metadata" / "config.json").read_bytes())

    except (OSError, ValueError):
        config = {}

    return stamps[-1].parent.name, config if isinstance(config, dict) else {}


def _config_dirs(config: Mapping[str, Any]) -> dict[str, Path]:
    """The dirs an installed config moved artifacts to.

    Args:
        config: The installed `config.json`.

    Returns:
        `DEFAULT_DIRS`, overridden by the config's `default`, `env` and `explicit` parts.
    """
    dirs = {**DEFAULT_DIRS}
    for part in ("default", "env", "explicit"):
        dirs |= _dir_values(config.get(part) or {})

    return dirs


def _dir_values(part: Mapping[str, Any]) -> dict[str, Path]:
    """A config part's dirs, the keys whose values are strings.

    Args:
        part: One of a config's `default`, `env` or `explicit` parts.

    Returns:
        The dirs, as paths.
    """
    return {k: Path(v) for k, v in part.items() if isinstance(v, str)}


def _locate(
    key: str, args: list[Any], staged: Path, env: BreweryENV, dirs: Mapping[str, Path]
) -> tuple[Path, Path]:
    """Resolve an artifact's source and target.

    The feed's sibling `target` is computed before placeholders are expanded, so
    it is wrong for absolute targets; the target is worked out from `args` instead.

    Args:
        key: The artifact's stanza key.
        args: The stanza's value: a source, then an optional `{"target": …}`.
        staged: The version dir.
        env: The Homebrew layout.
        dirs: The dirs moved artifacts go to.

    Returns:
        Tuple of (source, target).

    Raises:
        CellarError: If the artifact names no target brew could place.
    """

    def expand(value: str) -> str:
        return (
            value.replace("/$HOME", str(Path.home()))
            .replace("$HOMEBREW_PREFIX", str(env.prefix))
            .replace("$HOMEBREW_CELLAR", str(env.cellar))
            .replace("$APPDIR", str(dirs["appdir"]))
        )

    source = staged / expand(args[0])  # Absolute source replaces `staged`
    given = args[1].get("target") if len(args) > 1 else None
    name = expand(given) if given else source.name

    if key == "manpage":
        if not (section := _MANPAGE_SECTION.search(args[0])):
            raise CellarError(f"{args[0]} is not a man page name", path=source)

        return source, env.prefix / _LINKED[key] / f"man{section[1]}" / source.name

    if key == "bash_completion":
        name = Path(name).stem

    elif key == "zsh_completion" and not name.startswith("_"):
        name = f"_{Path(name).stem}"

    elif key == "fish_completion" and not name.endswith(".fish"):
        name = f"{Path(name).stem}.fish"

    target = Path(name).expanduser()
    if target.is_absolute():
        return source, target

    if key in _LINKED:
        return source, env.prefix / _LINKED[key] / target

    if base := _MOVED[key]:
        return source, dirs[base] / target

    raise CellarError(f"{key} {args[0]} has no absolute target", path=source)


def _move_out(source: Path, target: Path, *, reuse: bool = False) -> None:
    """Move an artifact to its target, leaving a symlink to it at the source.

    Args:
        source: The artifact in the version dir.
        target: Where it goes.
        reuse: Whether an upgrade left the target as an empty dir to move into.

    Raises:
        LinkError: If the target is already taken.
    """
    empty = reuse and target.is_dir() and not any(target.iterdir())
    if empty and not source.is_dir():
        target.rmdir()

    elif empty:
        _fill(source, target)
        # The kept dir has the old version's quarantine; brew's `copy_xattrs`
        if quarantine := _xattr("-p", QUARANTINE, str(source)).strip():
            _xattr("-w", QUARANTINE, quarantine, str(target))
        source.rmdir()
        source.symlink_to(target)
        _add_alt_name(target, source.name)
        return

    if os.path.lexists(target):
        raise LinkError([(str(target), "an existing file")])

    target.parent.mkdir(parents=True, exist_ok=True)
    _relocate(source, target)
    source.symlink_to(target)
    _add_alt_name(target, source.name)


def _add_alt_name(target: Path, name: str) -> None:
    """Tell Spotlight a renamed artifact's original name.

    Best-effort, unlike brew: cosmetic, so not worth failing the install.

    Args:
        target: The moved artifact.
        name: Its name in the cask.
    """
    if name.casefold() == target.name.casefold():
        return

    names = _xattr("-p", ALT_NAMES, str(target)).strip()
    if names.startswith("(") and names.endswith(")"):
        names = names[1:-1]

    # Some apps ship read-only, so make the top level writable first
    subprocess.run(
        ["chmod", "--", "u+rw", str(target), str(target.resolve())],
        capture_output=True,
        check=False,
    )
    _xattr("-w", ALT_NAMES, f'({names}{", " if names else ""}"{name}")', str(target))


def _xattr(*args: str) -> str:
    """Run `xattr`, best-effort.

    Args:
        *args: Its arguments.

    Returns:
        Its stdout, empty if it failed.
    """
    cmd = ["xattr", *args]

    return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout


def _link(source: Path, target: Path, cellar: Path, *, binary: bool) -> bool:
    """Symlink `target` to `source`, as brew's `Symlinked` does.

    Args:
        source: The file in the Caskroom or an installed app.
        target: The link to create.
        cellar: The Cellar, whose formulae keep links they already own.
        binary: Whether to make the source executable.

    Returns:
        Whether a link was created: False if it was already there or a formula
        owns the target, so rollback leaves it alone.

    Raises:
        LinkError: If anything else is at the target.
    """
    if target.exists():
        real = target.resolve()
        if real == source.resolve():
            return False  # Already linked

        if target.is_symlink() and real.is_relative_to(cellar.resolve()):
            log.warning(f"{target} is from a formula; skipping link")
            return False

        raise LinkError([(str(target), "an existing file")])

    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)  # brew's `ln -sfn` replaces a dangling link
    target.symlink_to(source)
    if binary and not os.access(source, os.X_OK):
        source.chmod(source.stat().st_mode | 0o111)

    return True


def _relocate(source: Path, target: Path) -> None:
    """Rename `source` to `target`, copying with ditto across volumes.

    Args:
        source: The path to move.
        target: Its new path, which must not exist.

    Raises:
        CellarError: If the move or cross-volume copy fails.
    """
    try:
        source.rename(target)

    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise CellarError(f"can't move {source} to {target}: {exc}") from exc

        try:
            _run(["ditto", str(source), str(target)])

        except ExtractionError as exc:
            # `target` didn't exist before, so whatever is there is ditto's partial copy
            shutil.rmtree(target, ignore_errors=True)
            target.unlink(missing_ok=True)  # The file or link rmtree can't remove
            raise CellarError(f"can't move {source} to {target}: {exc}") from exc

        if source.is_dir() and not source.is_symlink():
            shutil.rmtree(source)

        else:
            source.unlink()


def _unpack(download: Path, dest: Path, *, name: str) -> None:
    """Unpack a dmg, zip or tar into `dest`, or copy any other file in as `name`.

    The cache names files by digest, so the container is told by its contents.

    Args:
        download: The downloaded container.
        dest: The empty directory to unpack into.
        name: The filename a bare download is staged under.

    Raises:
        ExtractionError: If the container is one brewery can't open.
    """
    with download.open("rb") as fh:
        head = fh.read(512)
        fh.seek(max(download.stat().st_size - 512, 0))
        tail = fh.read(512)

    if head.startswith(b"PK\x03\x04"):
        _run(["ditto", "-x", "-k", str(download), str(dest)])

    elif tail.startswith(b"koly"):
        _unpack_dmg(download, dest)

    elif head.startswith(_TAR_MAGIC) or head[257:262] == b"ustar":
        extract_tar(download, dest)

    elif kind := _UNSUPPORTED.get(head[:4]):
        raise ExtractionError(f"can't unpack a {kind} archive", archive=download)

    else:
        shutil.copy2(download, dest / name)


def _unpack_dmg(dmg: Path, dest: Path) -> None:
    """Copy a disk image's contents into `dest`, minus volume furniture.

    Args:
        dmg: The disk image.
        dest: The directory to copy into.
    """
    with tempfile.TemporaryDirectory() as mount_dir:
        # "qn" pages past and declines a licence prompt
        out = _run(
            [
                "hdiutil", "attach", "-plist", "-nobrowse", "-readonly",
                "-mountrandom", mount_dir, str(dmg),
            ],
            stdin="qn\n",
        )  # fmt: skip
        mounts = [
            Path(e["mount-point"])
            for e in plistlib.loads(out.encode())["system-entities"]
            if "mount-point" in e
        ]

        try:
            for mount in mounts:
                for entry in mount.iterdir():
                    # The `Applications` shortcut points out of the image
                    if entry.name in _DMG_METADATA or (
                        entry.is_symlink() and os.readlink(entry).startswith("/")
                    ):
                        continue

                    _run(["ditto", str(entry), str(dest / entry.name)])

        finally:
            for mount in mounts:
                subprocess.run(
                    ["hdiutil", "detach", "-force", str(mount)],
                    capture_output=True,
                    check=False,
                )


def _url_name(url: str) -> str:
    """The download's filename, as the URL names it.

    Args:
        url: The cask's download URL.

    Returns:
        The decoded last path segment, or an empty string if there is none.
    """
    return unquote(PurePosixPath(urlsplit(url).path).name)


def _run(cmd: list[str], *, stdin: str | None = None) -> str:
    """Run one staging tool, mapping failure to ExtractionError.

    Args:
        cmd: The command to run.
        stdin: Text to feed the command, or None for no input.

    Returns:
        The command's stdout.

    Raises:
        ExtractionError: If the command exits non-zero or is not installed.
    """
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            check=False,
        )

    except OSError as exc:
        raise ExtractionError(f"{cmd[0]} failed: {exc}") from exc

    if proc.returncode != 0:
        raise ExtractionError(f"{cmd[0]} failed: {proc.stderr.strip()}")

    return proc.stdout
