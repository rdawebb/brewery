"""Run a cask's `uninstall` and `zap` directives.

macOS-only, like native casks; brew runs some steps with sudo; where one would
need root here, it raises instead, and the cask goes to brew.
"""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's cask uninstall directives.

from __future__ import annotations

import getpass
import glob
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from brewery.core.errors import CellarError
from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

# `ORDERED_DIRECTIVES`, less those `classify_uninstall` sends to brew
_ORDER = ("launchctl", "quit", "signal", "delete", "trash", "rmdir")

_RUNNING_APPS_JS = """
ObjC.import('AppKit')
function run() {
  var apps = $.NSWorkspace.sharedWorkspace.runningApplications, out = []
  for (var i = 0; i < apps.count; i++) {
    var app = apps.objectAtIndex(i), id = app.bundleIdentifier
    if (!id.isNil()) out.push(app.processIdentifier + '\\t' + ObjC.unwrap(id))
  }
  return out.join('\\n')
}
"""

_QUIT_JS = """
ObjC.import('stdlib')
function run(argv) {
  var app = Application(argv[0])
  try { app.quit() } catch (err) { if (app.running()) $.exit(1) }
  $.exit(0)
}
"""

_TRASH_JS = """
ObjC.import('Foundation')
function run(argv) {
  var fm = $.NSFileManager.defaultManager, failed = []
  argv.forEach(function (p) {
    var url = $.NSURL.fileURLWithPath(p)
    if (!fm.trashItemAtURLResultingItemURLError(url, null, null)) failed.push(p)
  })
  return failed.join('\\n')
}
"""


def run_directives(stanzas: Sequence[Mapping[str, Any]], *, deferred: bool) -> None:
    """Run the stanzas' directives, one directive at a time across all of them.

    Args:
        stanzas: The `uninstall` (and with `--zap`, `zap`) directive dicts, in order.
        deferred: True for `rmdir`, which runs once the artifacts are removed.

    Raises:
        CellarError: If a step needs root, or a path can't be deleted.
    """
    for name in _ORDER:
        if (name == "rmdir") != deferred:
            continue

        for stanza in stanzas:
            if name in stanza:
                _RUNNERS[name](_flat(stanza[name]))


def remove(path: Path) -> None:
    """Remove a file, link or directory tree, if it's there.

    Args:
        path: The path to remove.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _flat(value: Any) -> list[str]:
    """A directive's value as a flat list of strings.

    Args:
        value: The directive's value, possibly a nested list.

    Returns:
        The value as a flat list of strings.
    """
    if isinstance(value, list):
        return [s for v in value for s in _flat(v)]

    return [str(value)]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing its output and ignoring its exit status.

    Args:
        *args: The command and its arguments.

    Returns:
        The completed process, with text stdout and stderr.
    """
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _wildcard(pattern: str) -> re.Pattern[str]:
    """A `*` wildcard as an anchored, case-insensitive pattern.

    Args:
        pattern: The pattern, possibly with `*` wildcards.

    Returns:
        The compiled pattern, to use with `fullmatch`.
    """
    return re.compile(".*".join(map(re.escape, pattern.split("*"))), re.IGNORECASE)


def _running_apps() -> list[tuple[int, str]]:
    """The running applications, as (pid, bundle id).

    Returns:
        A (pid, bundle id) pair for each running app that has a bundle id.
    """
    out = _run("osascript", "-l", "JavaScript", "-e", _RUNNING_APPS_JS).stdout
    rows = (line.partition("\t") for line in out.splitlines())

    return [(int(pid), bid) for pid, _, bid in rows if pid.isdigit() and bid]


def _expand(bundle_id: str, apps: list[tuple[int, str]]) -> list[str]:
    """A bundle id, or the running apps' ids its `*` wildcard matches.

    Args:
        bundle_id: The bundle id, possibly with `*` wildcards.
        apps: The running apps, as (pid, bundle id).

    Returns:
        The bundle ids that match the pattern.
    """
    if "*" not in bundle_id:
        return [bundle_id]

    pattern = _wildcard(bundle_id)
    return [bid for _, bid in apps if pattern.fullmatch(bid)]


def _hosting(bundle_id: str, apps: list[tuple[int, str]]) -> bool:
    """Whether the app is one this process runs inside, such as its terminal.

    Args:
        bundle_id: The app's bundle id.
        apps: The running apps, as (pid, bundle id).

    Returns:
        True if this process is a descendant of one of the app's processes.
    """
    out = _run("ps", "-A", "-o", "pid=,ppid=").stdout
    parent = {int(p): int(pp) for p, pp in (line.split() for line in out.splitlines())}
    ancestors, pid = set(), os.getpid()
    while pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        pid = parent.get(pid, 0)

    return any(p in ancestors and b.lower() == bundle_id.lower() for p, b in apps)


def _gui() -> bool:
    """Whether this user is logged in at the console.

    Returns:
        True if `who` lists this user on the console.
    """
    user = getpass.getuser()
    return any(
        line.split()[:2] == [user, "console"]
        for line in _run("who").stdout.splitlines()
    )


def _is_running(bundle_id: str) -> bool:
    """Whether an app with this bundle id is running.

    Args:
        bundle_id: The app's bundle id, matched case-insensitively.

    Returns:
        True if a running app has this bundle id.
    """
    return any(b.lower() == bundle_id.lower() for _, b in _running_apps())


def _quit(bundle_ids: list[str]) -> None:
    """Ask each app to quit, waiting up to 10 seconds, as brew's `uninstall_quit` does.

    Args:
        bundle_ids: Bundle ids, possibly with `*` wildcards.
    """
    apps = _running_apps()
    for bundle_id in [b for pattern in bundle_ids for b in _expand(pattern, apps)]:
        if not _is_running(bundle_id):
            continue

        if not _gui():
            log.warning(f"Not logged into a GUI; skipping quitting {bundle_id}")
            continue

        if _hosting(bundle_id, apps):
            log.warning(f"Skipping quitting {bundle_id}: brewery is running inside it")
            continue

        deadline = time.monotonic() + 10
        while _run(
            "osascript", "-l", "JavaScript", "-e", _QUIT_JS, bundle_id
        ).returncode or (_is_running(bundle_id)):
            if time.monotonic() > deadline:
                log.warning(f"{bundle_id} did not quit")
                break


def _launchctl_list() -> list[tuple[int, str]]:
    """launchd's jobs in this user's domain, as (pid, label); pid 0 if not running.

    Returns:
        A (pid, label) pair for each job `launchctl list` shows.
    """
    rows = (
        line.split("\t") for line in _run("launchctl", "list").stdout.splitlines()[1:]
    )
    return [(int(r[0]) if r[0].isdigit() else 0, r[2]) for r in rows if len(r) == 3]


def _signal(values: list[str]) -> None:
    """Send each (signal, bundle id) pair to the app's processes.

    Only processes this user owns are signalled.

    Args:
        values: Signal names and bundle ids, alternating.

    Raises:
        CellarError: If a signal name is unknown or has no bundle id with it.
    """
    try:
        pairs = [
            (
                int(n)
                if n.isdigit()
                else signal.Signals[f"SIG{n.removeprefix('SIG')}"],
                p,
            )
            for n, p in zip(values[::2], values[1::2], strict=True)
        ]

    except (KeyError, ValueError) as exc:
        raise CellarError(f"bad signal directive {values}: {exc}") from exc

    apps = _running_apps()
    for signum, pattern in pairs:
        for bundle_id in _expand(pattern, apps):
            label = re.compile(
                rf"(?:application\.)?{re.escape(bundle_id)}(?:\.\d+){{0,2}}"
            )
            pids = [
                pid for pid, job in _launchctl_list() if pid and label.fullmatch(job)
            ]
            if not pids:
                continue

            if _hosting(bundle_id, apps):
                log.warning(
                    f"Skipping signalling {bundle_id}: brewery is running inside it"
                )
                continue

            for pid in pids:
                if _run("ps", "-o", "uid=", "-p", str(pid)).stdout.strip() != str(
                    os.getuid()
                ):
                    continue

                try:
                    os.kill(pid, signum)

                except OSError as exc:
                    log.warning(f"Failed to signal {bundle_id} ({pid}): {exc}")

            time.sleep(3)


def _launchctl(services: list[str]) -> None:
    """Remove launchd services and their plists.

    Args:
        services: Service labels, possibly with `*` wildcards.

    Raises:
        CellarError: If a service is in the system domain, which needs root.
    """
    labels = [
        label
        for service in services
        for label in (
            [
                job
                for pid, job in _launchctl_list()
                if pid and _wildcard(service).fullmatch(job)
            ]
            if "*" in service
            else [service]
        )
    ]
    uid = os.getuid()
    for label in labels:
        system = [
            Path("/Library") / d / f"{label}.plist"
            for d in ("LaunchAgents", "LaunchDaemons")
        ]
        if (
            any(p.exists() for p in system)
            or not _run("launchctl", "print", f"system/{label}").returncode
        ):
            raise CellarError(f"launchctl service {label} needs root")

        found = any(
            not _run("launchctl", "print", f"{domain}/{uid}/{label}").returncode
            for domain in ("gui", "user")
        ) or bool(
            (r := _run("launchctl", "list", label)).stdout.strip() and not r.returncode
        )
        if found and not _run("launchctl", "remove", label).returncode:
            time.sleep(1)

        for d in ("LaunchAgents", "LaunchDaemons"):
            (Path.home() / "Library" / d / f"{label}.plist").unlink(missing_ok=True)

        if label.startswith("/") and Path(label).exists():  # A path to a plist
            _run("launchctl", "unload", "-w", "--", label)
            Path(label).unlink(missing_ok=True)
            time.sleep(1)


def _braces(pattern: str) -> list[str]:
    """Expand `{a,b}` alternatives.

    Args:
        pattern: A glob pattern, possibly with `{a,b}` alternatives.

    Returns:
        The pattern once for each combination of alternatives.
    """
    if not (m := re.search(r"\{([^{}]*)\}", pattern)):
        return [pattern]

    head, tail = pattern[: m.start()], pattern[m.end() :]

    return [p for alt in m[1].split(",") for p in _braces(head + alt + tail)]


def _resolve(action: str, paths: Iterable[str]) -> Iterator[list[Path]]:
    """Resolve each path's glob matches.

    Relative paths, `.`/`..` segments and the paths brew never deletes are skipped.

    Args:
        action: The directive, for warnings.
        paths: The directive's paths, `~` meaning the home dir.

    Yields:
        The matches for each path that wasn't skipped.
    """
    home = str(Path.home())
    for raw in paths:
        path = re.sub(r"^~(?=/|$)", lambda _: home, raw)
        if not os.path.isabs(path) or {".", ".."} & set(path.split("/")):
            log.warning(f"Skipping {action} for {raw}: not an absolute path")
            continue

        matches = [
            Path(m) for p in _braces(path) for m in sorted(glob.glob(p, recursive=True))
        ]
        for m in [m for m in matches if undeletable(m)]:
            log.warning(f"Skipping {action} for undeletable path {m}")

        yield [m for m in matches if not undeletable(m)]


def _delete(paths: list[str]) -> None:
    """Remove the paths' matches.

    Args:
        paths: The `delete` paths.

    Raises:
        CellarError: If one can't be removed; brew removes it with sudo.
    """
    for matches in _resolve("delete", paths):
        for path in matches:
            try:
                remove(path)

            except OSError as exc:
                raise CellarError(f"can't delete {path}: {exc}", path=path) from exc


def _trash(paths: list[str]) -> None:
    """Move the paths' matches to the Trash, warning about any that fail.

    Args:
        paths: The `trash` paths.
    """
    matches = [str(p) for group in _resolve("trash", paths) for p in group]
    if not matches:
        return

    proc = _run("osascript", "-l", "JavaScript", "-e", _TRASH_JS, *matches)
    failed = matches if proc.returncode else proc.stdout.splitlines()
    if failed:
        log.warning(f"Couldn't trash these, please do so manually: {', '.join(failed)}")


def _rmdir(paths: list[str]) -> None:
    """Remove each path's matching dirs where they hold nothing but empty dirs.

    Args:
        paths: The `rmdir` paths.
    """
    for matches in _resolve("rmdir", paths):
        if all(p.is_dir() for p in matches):
            _rmdir_all(matches)


def _rmdir_all(dirs: Iterable[Path]) -> bool:
    """Remove empty dir trees, stopping at the first that isn't.

    A `.DS_Store` doesn't count against a dir being empty.

    Args:
        dirs: The dirs to remove.

    Returns:
        Whether all of them were removed.
    """
    for d in dirs:
        try:
            if d.is_symlink():
                return False

            children = list(d.iterdir())
            if not all(c.is_dir() or c.name == ".DS_Store" for c in children):
                return False

            if (ds_store := d / ".DS_Store") in children:
                ds_store.unlink()
                children.remove(ds_store)

            if not _rmdir_all(children):
                return False

            d.rmdir()

        except OSError:
            return False  # brew retries an unreadable dir with sudo; leaving it is safe

    return True


_RUNNERS = {
    "launchctl": _launchctl,
    "quit": _quit,
    "signal": _signal,
    "delete": _delete,
    "trash": _trash,
    "rmdir": _rmdir,
}


def undeletable(path: Path) -> bool:
    """Whether brew would refuse to remove this path.

    Args:
        path: An absolute path.

    Returns:
        True for system and home dirs no cask may remove.
    """
    path = Path(os.path.abspath(path))
    if str(path) in _SYSTEM_DIRS:
        return True

    home = Path(os.path.abspath(Path.home()))

    return path.is_relative_to(home) and str(path.relative_to(home)) in _HOME_DIRS


# `SYSTEM_DIRS` and `UNDELETABLE_PATHS` (cask/macos.rb); home dirs are under ~
_SYSTEM_DIRS = frozenset(
    """
/
/Applications
/Applications/Utilities
/Incompatible Software
/Library
/Library/Application Support
/Library/Audio
/Library/Caches
/Library/ColorPickers
/Library/ColorSync
/Library/Components
/Library/Compositions
/Library/Contextual Menu Items
/Library/CoreMediaIO
/Library/Desktop Pictures
/Library/Developer
/Library/Dictionaries
/Library/DirectoryServices
/Library/Documentation
/Library/Extensions
/Library/Filesystems
/Library/Fonts
/Library/Frameworks
/Library/Graphics
/Library/Image Capture
/Library/Input Methods
/Library/Internet Plug-Ins
/Library/Java
/Library/Java/Extensions
/Library/Java/JavaVirtualMachines
/Library/Keyboard Layouts
/Library/Keychains
/Library/LaunchAgents
/Library/LaunchDaemons
/Library/Logs
/Library/Messages
/Library/Modem Scripts
/Library/OpenDirectory
/Library/PDF Services
/Library/Perl
/Library/PreferencePanes
/Library/Preferences
/Library/Printers
/Library/PrivilegedHelperTools
/Library/Python
/Library/QuickLook
/Library/QuickTime
/Library/Receipts
/Library/Ruby
/Library/Sandbox
/Library/Screen Savers
/Library/ScriptingAdditions
/Library/Scripts
/Library/Security
/Library/Speech
/Library/Spelling
/Library/Spotlight
/Library/StartupItems
/Library/SystemProfiler
/Library/Updates
/Library/User Pictures
/Library/Video
/Library/WebServer
/Library/Widgets
/Library/iTunes
/Network
/System
/System/Library
/System/Library/Accessibility
/System/Library/Accounts
/System/Library/Address Book Plug-Ins
/System/Library/Assistant
/System/Library/Automator
/System/Library/BridgeSupport
/System/Library/Caches
/System/Library/ColorPickers
/System/Library/ColorSync
/System/Library/Colors
/System/Library/Components
/System/Library/Compositions
/System/Library/CoreServices
/System/Library/DTDs
/System/Library/DirectoryServices
/System/Library/Displays
/System/Library/Extensions
/System/Library/Filesystems
/System/Library/Filters
/System/Library/Fonts
/System/Library/Frameworks
/System/Library/Graphics
/System/Library/IdentityServices
/System/Library/Image Capture
/System/Library/Input Methods
/System/Library/InternetAccounts
/System/Library/Java
/System/Library/KerberosPlugins
/System/Library/Keyboard Layouts
/System/Library/Keychains
/System/Library/LaunchAgents
/System/Library/LaunchDaemons
/System/Library/LinguisticData
/System/Library/LocationBundles
/System/Library/LoginPlugins
/System/Library/Messages
/System/Library/Metadata
/System/Library/MonitorPanels
/System/Library/OpenDirectory
/System/Library/OpenSSL
/System/Library/Password Server Filters
/System/Library/PerformanceMetrics
/System/Library/Perl
/System/Library/PreferencePanes
/System/Library/Printers
/System/Library/PrivateFrameworks
/System/Library/QuickLook
/System/Library/QuickTime
/System/Library/QuickTimeJava
/System/Library/Recents
/System/Library/SDKSettingsPlist
/System/Library/Sandbox
/System/Library/Screen Savers
/System/Library/ScreenReader
/System/Library/ScriptingAdditions
/System/Library/ScriptingDefinitions
/System/Library/Security
/System/Library/Services
/System/Library/Sounds
/System/Library/Speech
/System/Library/Spelling
/System/Library/Spotlight
/System/Library/StartupItems
/System/Library/SyncServices
/System/Library/SystemConfiguration
/System/Library/SystemProfiler
/System/Library/Tcl
/System/Library/TextEncodings
/System/Library/User Template
/System/Library/UserEventPlugins
/System/Library/Video
/System/Library/WidgetResources
/User Information
/Users
/Volumes
/bin
/boot
/cores
/dev
/etc
/etc/X11
/etc/opt
/etc/sgml
/etc/xml
/home
/libexec
/lost+found
/media
/mnt
/net
/opt
/private
/private/etc
/private/tftpboot
/private/tmp
/private/var
/proc
/root
/sbin
/srv
/tmp
/usr
/usr/X11R6
/usr/bin
/usr/etc
/usr/include
/usr/lib
/usr/libexec
/usr/libexec/cups
/usr/local
/usr/local/Cellar
/usr/local/Frameworks
/usr/local/Library
/usr/local/bin
/usr/local/etc
/usr/local/include
/usr/local/lib
/usr/local/libexec
/usr/local/opt
/usr/local/share
/usr/local/share/man
/usr/local/share/man/man1
/usr/local/share/man/man2
/usr/local/share/man/man3
/usr/local/share/man/man4
/usr/local/share/man/man5
/usr/local/share/man/man6
/usr/local/share/man/man7
/usr/local/share/man/man8
/usr/local/share/man/man9
/usr/local/share/man/mann
/usr/local/var
/usr/local/var/lib
/usr/local/var/lock
/usr/local/var/run
/usr/sbin
/usr/share
/usr/share/man
/usr/share/man/man1
/usr/share/man/man2
/usr/share/man/man3
/usr/share/man/man4
/usr/share/man/man5
/usr/share/man/man6
/usr/share/man/man7
/usr/share/man/man8
/usr/share/man/man9
/usr/share/man/mann
/usr/src
/var
/var/cache
/var/lib
/var/lock
/var/log
/var/mail
/var/run
/var/spool
/var/spool/mail
/var/tmp
""".strip().splitlines()
)

_HOME_DIRS = frozenset(
    """
.
Applications
Applications/.localized
Desktop
Desktop/.localized
Documents
Documents/.localized
Downloads
Downloads/.localized
Mail
Movies
Movies/.localized
Music
Music/.localized
Music/iTunes
Music/iTunes/iTunes Music
Music/iTunes/Album Artwork
News
Pictures
Pictures/.localized
Pictures/Desktops
Pictures/Photo Booth
Pictures/iChat Icons
Pictures/iPhoto Library
Public
Public/.localized
Sites
Sites/.localized
Library
Library/.localized
Library/Accessibility
Library/Accounts
Library/Address Book Plug-Ins
Library/Application Scripts
Library/Application Support
Library/Application Support/Apple
Library/Application Support/com.apple.AssistiveControl
Library/Application Support/com.apple.QuickLook
Library/Application Support/com.apple.TCC
Library/Assistants
Library/Audio
Library/Automator
Library/Autosave Information
Library/Caches
Library/Calendars
Library/ColorPickers
Library/ColorSync
Library/Colors
Library/Components
Library/Compositions
Library/Containers
Library/Contextual Menu Items
Library/Cookies
Library/DTDs
Library/Desktop Pictures
Library/Developer
Library/Dictionaries
Library/DirectoryServices
Library/Displays
Library/Documentation
Library/Extensions
Library/Favorites
Library/FileSync
Library/Filesystems
Library/Filters
Library/FontCollections
Library/Fonts
Library/Frameworks
Library/GameKit
Library/Graphics
Library/Group Containers
Library/Icons
Library/IdentityServices
Library/Image Capture
Library/Images
Library/Input Methods
Library/Internet Plug-Ins
Library/InternetAccounts
Library/iTunes
Library/KeyBindings
Library/Keyboard Layouts
Library/Keychains
Library/LaunchAgents
Library/LaunchDaemons
Library/LocationBundles
Library/LoginPlugins
Library/Logs
Library/Mail
Library/Mail Downloads
Library/Messages
Library/Metadata
Library/Mobile Documents
Library/MonitorPanels
Library/OpenDirectory
Library/PDF Services
Library/PhonePlugins
Library/Phones
Library/PreferencePanes
Library/Preferences
Library/Printers
Library/PrivateFrameworks
Library/PubSub
Library/QuickLook
Library/QuickTime
Library/Receipts
Library/Recent Servers
Library/Recents
Library/Safari
Library/Saved Application State
Library/Screen Savers
Library/ScreenReader
Library/ScriptingAdditions
Library/ScriptingDefinitions
Library/Scripts
Library/Security
Library/Services
Library/Sounds
Library/Speech
Library/Spelling
Library/Spotlight
Library/StartupItems
Library/StickiesDatabase
Library/Sync Services
Library/SyncServices
Library/SyncedPreferences
Library/TextEncodings
Library/User Pictures
Library/Video
Library/Voices
Library/WebKit
Library/WidgetResources
Library/Widgets
Library/Workflows
""".strip().splitlines()
)
