"""Link a Cellar keg's contents into the Homebrew prefix.

Conflict detection is a pre-pass: nothing is mutated if the link would conflict,
so the caller can fall back to `brew link` without a partially linked prefix.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from brewery.core.errors import BrewError

# Top-level keg directories
_ELIGIBLE = ("bin", "sbin", "etc", "include", "lib", "share", "Frameworks")

# Files brew refuses to link
_LIB_SKIP_FILE = frozenset({"charset.alias"})

# Exact dir names: mkpath ONLY at top level; their subdirs are linked whole.
_LIB_EXACT = frozenset({"cps", "pkgconfig", "cmake", "dtrace", "ghc", "php"})

# Prefix families: matched (^-anchored) against the FULL relative path
_LIB_PREFIX_RX = re.compile(
    r"^(gdk-pixbuf|gio|lua|mecab|node|ocaml|perl5|postgresql@\d+"
    r"|pypy|python[23]\.\d+|R|ruby)"
)

_INFOFILE_RX = re.compile(r"info/([^.].*?\.info(\.gz)?|dir)$")  # :info -> a link
_LOCALEDIR_RX = re.compile(
    r"(locale|man)/([a-z]{2}|C|POSIX)(_[A-Z]{2})?(\.[a-zA-Z\-0-9]+(@.+)?)?"
)
_SHARE_SKIP_FILE = frozenset({"locale/locale.alias"})
_SHARE_SKIP_RX = re.compile(r"^icons/.*/icon-theme\.cache$")

# ^-anchored prefix regexes brew mkpaths
_SHARE_PREFIX_RX = re.compile(r"^(icons/|zsh|fish|lua/|guile/|postgresql@\d+|pypy)")

# Relative paths that are always real dirs
_SHARE_PATHS = frozenset(
    {
        "aclocal",
        "cps",
        "doc",
        "info",
        "java",
        "locale",
        "man",
        "man/man1",
        "man/man2",
        "man/man3",
        "man/man4",
        "man/man5",
        "man/man6",
        "man/man7",
        "man/man8",
        "man/cat1",
        "man/cat2",
        "man/cat3",
        "man/cat4",
        "man/cat5",
        "man/cat6",
        "man/cat7",
        "man/cat8",
        "applications",
        "gnome",
        "gnome/help",
        "icons",
        "mime",
        "mime/packages",
        "mime-info",
        "pixmaps",
        "postgresql",
        "sounds",
    }
)


# mkpath only `.framework` and `.framework/Versions`; link everything else
_FRAMEWORK_RX = re.compile(r"[^/]*\.framework(/Versions)?$")

_LINKED_RECORD_DIR = "var/homebrew/linked"
_PYC_EXT = (".pyc", ".pyo")


class LinkError(BrewError):
    """Linking the keg conflicts with existing files; per-formula fallback signal."""

    def __init__(self, conflicts: list[tuple[str, str]]) -> None:
        """Initialise the LinkError with a list of conflicts.

        Args:
            conflicts: A list of tuples containing the destination and existing file paths.
        """
        self.conflicts = conflicts
        listing = "\n".join(
            f"  {dst} -> already {existing}" for dst, existing in conflicts
        )
        super().__init__(f"link conflicts:\n{listing}")


class Action(Enum):
    """Action to take for each file/directory."""

    SKIP = "skip"
    MKPATH = "mkpath"
    LINK = "link"


@dataclass
class LinkResult:
    """Result of a linking operation."""

    linked: list[str] = field(default_factory=list)  # Relative prefix paths symlinked
    created_dirs: list[str] = field(default_factory=list)  # mkpath'd dirs
    already_linked: list[str] = field(
        default_factory=list
    )  # Already pointing at this keg


def _strategy_lib(rel: Path, is_dir: bool) -> Action:
    """Determine the linking strategy for a library file or directory.

    Args:
        rel: The relative path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    posix = rel.as_posix()
    if not is_dir:
        return Action.SKIP if posix in _LIB_SKIP_FILE else Action.LINK

    # Exact names mkpath only the top level; prefix families the whole subtree
    if posix in _LIB_EXACT or _LIB_PREFIX_RX.match(posix):
        return Action.MKPATH

    return Action.LINK


def _strategy_share(rel: Path, is_dir: bool) -> Action:
    """Determine the linking strategy for a shared file or directory.

    Args:
        rel: The relative path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    posix = rel.as_posix()

    if posix in _SHARE_SKIP_FILE or _SHARE_SKIP_RX.search(posix):
        return Action.SKIP

    if not is_dir:
        # Includes INFOFILE matches: brew runs install-info on them, but for the
        # purpose of the prefix link they are ordinary relative symlinks.
        return Action.LINK

    if (
        posix in _SHARE_PATHS
        or _LOCALEDIR_RX.search(posix)
        or _SHARE_PREFIX_RX.match(posix)
    ):
        return Action.MKPATH

    return Action.LINK


def _strategy_etc(rel: Path, is_dir: bool) -> Action:
    """Determine the linking strategy for an etc file or directory.

    Args:
        rel: The relative path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    # etc directories are shared; files are linked
    return Action.MKPATH if is_dir else Action.LINK


def _strategy_framework(rel: Path, is_dir: bool) -> Action:
    """Determine the linking strategy for a framework file or directory.

    Args:
        rel: The relative path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    # Only the .framework bundle and its Versions dir are shared
    if is_dir and _FRAMEWORK_RX.search(rel.as_posix()):
        return Action.MKPATH

    return Action.LINK


def _strategy_link_all(rel: Path, is_dir: bool) -> Action:
    """Determine the linking strategy for all files or directories.

    Args:
        rel: The relative path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    return Action.LINK


_STRATEGIES = {
    "bin": _strategy_link_all,
    "sbin": _strategy_link_all,
    "include": _strategy_link_all,
    "Frameworks": _strategy_framework,
    "etc": _strategy_etc,
    "lib": _strategy_lib,
    "share": _strategy_share,
}


@dataclass
class _Plan:
    """Plan for linking files and directories."""

    keg: Path
    prefix: Path
    links: list[tuple[Path, Path]] = field(default_factory=list)  # (dst, src)
    dirs: list[Path] = field(default_factory=list)
    already: list[Path] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)

    def consider_link(self, dst: Path, src: Path, *, preserve_existing: bool) -> None:
        """Consider linking a source file or directory to a destination.

        Args:
            dst: The destination path.
            src: The source path.
            preserve_existing: Whether to preserve existing files.
        """
        if dst.is_symlink():
            if os.path.realpath(dst) == os.path.realpath(src):
                self.already.append(dst)  # Already linked to this keg

            else:
                self.conflicts.append((str(dst), os.readlink(dst)))

        elif dst.exists():
            if preserve_existing:
                self.already.append(dst)  # etc: keep the user's file

            else:
                self.conflicts.append((str(dst), "an existing file"))

        else:
            self.links.append((dst, src))


def _walk(
    src_dir: Path,
    sub_root: Path,
    strategy,
    plan: _Plan,
    *,
    preserve_existing: bool,
    skip_abs_symlinks: bool,
) -> None:
    """Walk the source directory and apply the linking strategy.

    Args:
        src_dir: The source directory to walk.
        sub_root: The subdirectory to use as the root for relative paths.
        strategy: The linking strategy to apply.
        plan: The plan to modify with the results of the walk.
        preserve_existing: Whether to preserve existing files.
        skip_abs_symlinks: Whether to skip absolute symlinks.
    """
    for entry in sorted(src_dir.iterdir()):
        if entry.name == ".DS_Store":
            continue

        rel = entry.relative_to(sub_root)
        dst = plan.prefix / entry.relative_to(plan.keg)
        is_symlink = entry.is_symlink()

        # brew does not link a bin/sbin symlink whose target is absolute
        if skip_abs_symlinks and is_symlink and os.path.isabs(os.readlink(entry)):
            continue

        is_dir = entry.is_dir() and not is_symlink

        if not is_dir:
            # brew prunes cached bytecode under site-packages (Python rewrites it)
            if entry.suffix in _PYC_EXT and "/site-packages/" in entry.as_posix():
                continue

        elif entry.suffix == ".app":
            continue  # brew never links .app bundles into the prefix

        action = strategy(rel, is_dir)

        if action is Action.SKIP:
            continue

        # A directory whose prefix path already exists as a real (non-symlink)
        # dir is descended into, walking the rest of the tree, regardless of the
        # strategy's verdict.
        descend = is_dir and (
            action is Action.MKPATH or (dst.is_dir() and not dst.is_symlink())
        )

        if descend:
            if action is Action.MKPATH:
                plan.dirs.append(dst)
            _walk(
                entry,
                sub_root,
                strategy,
                plan,
                preserve_existing=preserve_existing,
                skip_abs_symlinks=skip_abs_symlinks,
            )

        else:  # LINK (whole dir or file), or a file under a mkpath dir
            plan.consider_link(dst, entry, preserve_existing=preserve_existing)


def _build_plan(keg: Path, prefix: Path) -> _Plan:
    """Build a plan for linking the keg's contents into the prefix.

    Args:
        keg: The keg directory to link.
        prefix: The prefix directory to link into.

    Returns:
        A plan for linking the keg's contents into the prefix.
    """
    plan = _Plan(keg=keg, prefix=prefix)
    for sub in _ELIGIBLE:
        src = keg / sub
        if not (src.is_dir() and not src.is_symlink()):
            continue

        plan.dirs.append(prefix / sub)  # The eligible root is always a real dir
        _walk(
            src,
            src,
            _STRATEGIES[sub],
            plan,
            preserve_existing=(sub == "etc"),
            skip_abs_symlinks=(sub in ("bin", "sbin")),
        )

    return plan


def _make_relative_symlink(dst: Path, src: Path) -> None:
    """Create a relative symlink.

    Args:
        dst: The destination path.
        src: The source path.
    """
    target = os.path.relpath(src, dst.parent)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        dst.unlink()

    dst.symlink_to(target)


def _write_linked_record(prefix: Path, name: str, keg: Path) -> None:
    """Write a record of the linked keg.

    Args:
        prefix: The prefix path.
        name: The name of the linked keg.
        keg: The keg path.
    """
    record = prefix / _LINKED_RECORD_DIR / name
    record.parent.mkdir(parents=True, exist_ok=True)
    if record.is_symlink() or record.exists():
        record.unlink()

    record.symlink_to(os.path.relpath(keg, record.parent))


def link_keg(
    keg_dir: Path,
    *,
    prefix: Path,
    name: str,
    keg_only: bool = False,
    overwrite: bool = False,
) -> LinkResult:
    """Symlink the keg's contents into the prefix, brew-style.

    Returns a LinkResult describing what was linked. Raises LinkError if any
    target conflicts with a different keg or a real file (unless overwrite),
    having mutated nothing.

    Args:
        keg_dir: The keg directory to link.
        prefix: The prefix directory to link into.
        name: The name of the linked keg.
        keg_only: Whether to link only the keg.
        overwrite: Whether to overwrite existing links.

    Returns:
        A LinkResult describing what was linked.
    """
    if keg_only:
        return LinkResult()  # Keg-only formulae are never linked

    plan = _build_plan(keg_dir, prefix)

    if plan.conflicts and not overwrite:
        raise LinkError(plan.conflicts)

    result = LinkResult()
    for d in plan.dirs:
        d.mkdir(parents=True, exist_ok=True)
        result.created_dirs.append(d.relative_to(prefix).as_posix())

    # Symlink the non-conflicting targets
    for dst, src in plan.links:
        _make_relative_symlink(dst, src)
        result.linked.append(dst.relative_to(prefix).as_posix())

    # Under overwrite, replace the conflicting targets too
    if overwrite and plan.conflicts:
        for dst_str, _existing in plan.conflicts:
            dst = Path(dst_str)

            # Map the prefix path back to its keg source
            src = keg_dir / dst.relative_to(prefix)
            _make_relative_symlink(dst, src)
            result.linked.append(dst.relative_to(prefix).as_posix())

    for dst in plan.already:
        result.already_linked.append(dst.relative_to(prefix).as_posix())

    _write_linked_record(prefix, name, keg_dir)

    return result
