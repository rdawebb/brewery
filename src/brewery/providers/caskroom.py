"""Stage a cask's download into the Caskroom, laid out as brew lays it out.

macOS-only, like native casks themselves: `classify` sends every other host to brew.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from brewery.core.catalog import CaskRow
from brewery.core.errors import ExtractionError
from brewery.providers.extractor import extract_tar
from brewery.providers.receipt import RECEIPT_NAME, current_arch, dumps_bytes

QUARANTINE = "com.apple.quarantine"

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

# Archives brewery can't open; raising hands the cask back to brew
_UNSUPPORTED = {b"7z\xbc\xaf": "7z", b"Rar!": "rar", b"xar!": "xar"}

_TAR_MAGIC = (b"\x1f\x8b", b"BZh", b"\xfd7zXZ", b"\x28\xb5\x2f\xfd")


def stage(row: CaskRow, download: Path, caskroom: Path) -> Path:
    """Unpack `download` into `Caskroom/<token>/<version>/` and record it as brew does.

    The download is quarantined, and the quarantine copied onto everything
    staged from it; brew reads the version from `.metadata/<version>/` and the
    artifacts to uninstall from `.metadata/INSTALL_RECEIPT.json`.

    Args:
        row: The cask's catalog row.
        download: The verified download, from the downloader's cache.
        caskroom: The Caskroom directory.

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
        _write_metadata(row, token_dir)

    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    return staged


def _write_metadata(row: CaskRow, token_dir: Path) -> None:
    """Write the installed caskfile and receipt brew 7 reads for an API cask.

    Args:
        row: The cask's catalog row.
        token_dir: The cask's Caskroom directory.
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
        "installed_as_dependency": False,
        "installed_on_request": True,
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
