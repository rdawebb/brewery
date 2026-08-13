"""Link a Cellar keg's contents into the Homebrew prefix.

Conflict detection is a pre-pass: nothing is mutated if the link would conflict,
so the caller can fall back to `brew link` without a partially linked prefix.

Planning and applying run under one hold of the in-process structure lock, so a
peer thread cannot retype a shared directory in between and leave the plan
describing a prefix that never existed.
"""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg linking logic.

from __future__ import annotations

import contextlib
import os
import re
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import orjson

from brewery.core.errors import LinkError
from brewery.core.locks import structure_lock

# Serialises the link operations that mutate ownership of shared prefix directories;
# paired with `structure_lock`, which extends the same exclusion across processes
_STRUCTURE_LOCK = threading.RLock()

# Top-level keg directories
_ELIGIBLE = ("bin", "sbin", "etc", "include", "lib", "share", "Frameworks")

# Files brew refuses to link
_LIB_SKIP_FILE = frozenset({"charset.alias"})

# Exact dir names: mkpath ONLY at top level; their subdirs are linked whole
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

# Stored symlink set for fast unlinking
_LINK_MANIFEST = ".brewery_links.json"

# `consider_link`'s wording for a non-symlink occupying a link target
_EXISTING_FILE = "an existing file"

# Bounds the create/arbitrate cycle when a peer keeps taking and dropping a path
_LINK_RACE_RETRIES = 3


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
    conflicts: list[tuple[str, str]] = field(
        default_factory=list
    )  # (dst, existing) - only populated by a dry run


@dataclass
class UnlinkResult:
    """Result of unlinking a keg from the prefix."""

    removed: list[str] = field(default_factory=list)  # Relative prefix paths unlinked
    pruned: list[str] = field(default_factory=list)  # Emptied dirs removed
    scanned: bool = False  # Fell back to a filesystem scan


def _lstat(path: str) -> os.stat_result | None:
    """`os.lstat`, reporting an absent path as None rather than raising.

    Args:
        path: The path to stat, without following a final symlink.

    Returns:
        The stat result, or None if nothing occupies `path`.
    """
    try:
        return os.lstat(path)

    except (FileNotFoundError, NotADirectoryError):
        return None


def _strategy_lib(rel: str, is_dir: bool) -> Action:
    """Determine the linking strategy for a library file or directory.

    Args:
        rel: The relative posix path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    if not is_dir:
        return Action.SKIP if rel in _LIB_SKIP_FILE else Action.LINK

    # Exact names mkpath only the top level; prefix families the whole subtree
    if rel in _LIB_EXACT or _LIB_PREFIX_RX.match(rel):
        return Action.MKPATH

    return Action.LINK


def _strategy_share(rel: str, is_dir: bool) -> Action:
    """Determine the linking strategy for a shared file or directory.

    Args:
        rel: The relative posix path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    if rel in _SHARE_SKIP_FILE or _SHARE_SKIP_RX.search(rel):
        return Action.SKIP

    if not is_dir:
        # Includes INFOFILE matches: brew runs install-info on them, but for the
        # purpose of the prefix link they are ordinary relative symlinks
        return Action.LINK

    if rel in _SHARE_PATHS or _LOCALEDIR_RX.search(rel) or _SHARE_PREFIX_RX.match(rel):
        return Action.MKPATH

    return Action.LINK


def _strategy_etc(rel: str, is_dir: bool) -> Action:
    """Determine the linking strategy for an etc file or directory.

    Args:
        rel: The relative posix path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    # etc directories are shared; files are linked
    return Action.MKPATH if is_dir else Action.LINK


def _strategy_framework(rel: str, is_dir: bool) -> Action:
    """Determine the linking strategy for a framework file or directory.

    Args:
        rel: The relative posix path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    # Only the .framework bundle and its Versions dir are shared
    if is_dir and _FRAMEWORK_RX.search(rel):
        return Action.MKPATH

    return Action.LINK


def _strategy_link_all(rel: str, is_dir: bool) -> Action:
    """Determine the linking strategy for all files or directories.

    Args:
        rel: The relative posix path of the file or directory.
        is_dir: Whether the path is a directory.

    Returns:
        The action to take for the file or directory.
    """
    return Action.LINK


# Decides one entry from its path relative to the eligible root, plus its dir-ness
_Strategy = Callable[[str, bool], Action]

# Load-bearing for concurrency: every strategy decides from the relative path alone,
# so MKPATH-ness is identical for every keg regardless of prefix state. Explosion
# targets are thus never MKPATH paths, which keeps a peer process out of the
# directories the shared pass owns
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

    keg: str
    prefix: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (rel, src) files
    dir_links: list[tuple[str, str]] = field(
        default_factory=list
    )  # (rel, src) whole-dir symlinks (shared, applied under the lock)
    dirs: list[str] = field(default_factory=list)  # rel
    already: list[str] = field(default_factory=list)  # rel
    conflicts: list[tuple[str, str]] = field(  # (absolute dst, reason), user-facing
        default_factory=list
    )
    explosions: list[tuple[str, str]] = field(default_factory=list)  # (rel, src)
    # True once the walk links into a shared (non-mkpath) directory
    touches_shared: bool = False

    def consider_link(
        self,
        rel: str,
        dst: str,
        src: str,
        *,
        preserve_existing: bool,
        is_dir: bool,
        src_is_symlink: bool,
        dst_stat: os.stat_result | None,
    ) -> None:
        """Consider linking a source file or directory to a destination.

        Args:
            rel: The destination's prefix-relative posix path.
            dst: The absolute destination path.
            src: The absolute source path.
            preserve_existing: Whether to preserve existing files.
            is_dir: Whether `src` is a real directory (a whole-directory symlink).
            src_is_symlink: Whether `src` is a symlink.
            dst_stat: `lstat` of `dst`, or None if nothing occupies it.
        """
        exists = dst_stat is not None
        is_link = dst_stat is not None and stat.S_ISLNK(dst_stat.st_mode)

        # dst resolves to the same real path as src (e.g. metapackages)
        if exists and os.path.realpath(dst) == os.path.realpath(src):
            self.already.append(rel)
            return

        # A keg symlink aimed at its own prefix destination links to itself
        if not exists and src_is_symlink and _symlink_dest(src) == dst:
            return

        if is_link:
            real_dst = os.path.realpath(dst)
            if is_dir and os.path.isdir(real_dst):
                # Pre-check for unsolvable collisions between the displaced and new keg
                collisions = _merge_collisions(Path(dst), Path(real_dst), Path(src))
                if collisions:
                    self.conflicts.extend(collisions)

                else:
                    self.explosions.append((rel, src))
                    self.touches_shared = True

            else:
                self.conflicts.append((dst, os.readlink(dst)))

        elif exists:
            if preserve_existing:
                self.already.append(rel)  # etc: keep the user's file

            else:
                self.conflicts.append((dst, _EXISTING_FILE))

        elif is_dir:
            self.dir_links.append((rel, src))
            self.touches_shared = True

        else:
            self.links.append((rel, src))


def _walk(
    src_dir: str,
    rel_dir: str,
    cut: int,
    strategy: _Strategy,
    plan: _Plan,
    *,
    preserve_existing: bool,
    skip_abs_symlinks: bool,
) -> None:
    """Walk the source directory and apply the linking strategy.

    Args:
        src_dir: The absolute source directory to walk.
        rel_dir: The prefix-relative posix path of `src_dir` (e.g. "lib/pkgconfig").
        cut: Length of the eligible root plus its separator, so that `rel[cut:]` is
            the path relative to that root, which is what the strategies match on.
        strategy: The linking strategy to apply.
        plan: The plan to modify with the results of the walk.
        preserve_existing: Whether to preserve existing files.
        skip_abs_symlinks: Whether to skip absolute symlinks.
    """
    with os.scandir(src_dir) as it:
        entries = sorted(it, key=lambda e: e.name)

    for entry in entries:
        name = entry.name
        if name == ".DS_Store":
            continue

        src = entry.path
        rel = f"{rel_dir}/{name}"
        is_symlink = entry.is_symlink()

        # brew does not link a bin/sbin symlink whose target is absolute
        if skip_abs_symlinks and is_symlink and os.path.isabs(os.readlink(src)):
            continue

        is_dir = entry.is_dir(follow_symlinks=False)

        if not is_dir:
            # brew prunes cached bytecode under site-packages (Python rewrites it)
            if name.endswith(_PYC_EXT) and "/site-packages/" in src:
                continue

        elif name.endswith(".app"):
            continue  # brew never links .app bundles into the prefix

        action = strategy(rel[cut:], is_dir)

        if action is Action.SKIP:
            continue

        # A shared directory is descended into without consulting the prefix at all
        if is_dir and action is Action.MKPATH:
            plan.dirs.append(rel)

        else:
            dst = f"{plan.prefix}/{rel}"
            dst_stat = _lstat(dst)
            real_dir = dst_stat is not None and stat.S_ISDIR(dst_stat.st_mode)

            if not (is_dir and real_dir):
                # LINK (whole dir or file), or a file under a mkpath dir
                plan.consider_link(
                    rel,
                    dst,
                    src,
                    preserve_existing=preserve_existing,
                    is_dir=is_dir,
                    src_is_symlink=is_symlink,
                    dst_stat=dst_stat,
                )
                continue

            # Forced descent into a directory a peer keg already exploded into a real dir
            plan.touches_shared = True

        _walk(
            src,
            rel,
            cut,
            strategy,
            plan,
            preserve_existing=preserve_existing,
            skip_abs_symlinks=skip_abs_symlinks,
        )


def _walk_opts(sub: str) -> tuple[bool, bool]:
    """Per-root walk options for an eligible top-level dir.

    Args:
        sub: The eligible root name (e.g. "etc", "bin").

    Returns:
        (preserve_existing, skip_abs_symlinks) for that root.
    """
    return sub == "etc", sub in ("bin", "sbin")


def _build_plan(keg: Path, prefix: Path) -> _Plan:
    """Build a plan for linking the keg's contents into the prefix.

    Args:
        keg: The keg directory to link.
        prefix: The prefix directory to link into.

    Returns:
        A plan for linking the keg's contents into the prefix.
    """
    # Normalised once here; every path below is derived from these
    plan = _Plan(keg=str(keg), prefix=str(prefix))
    for sub in _ELIGIBLE:
        src = f"{plan.keg}/{sub}"
        src_stat = _lstat(src)
        if src_stat is None or not stat.S_ISDIR(src_stat.st_mode):
            continue

        plan.dirs.append(sub)  # The eligible root is always a real dir
        preserve_existing, skip_abs_symlinks = _walk_opts(sub)
        _walk(
            src,
            sub,
            len(sub) + 1,
            _STRATEGIES[sub],
            plan,
            preserve_existing=preserve_existing,
            skip_abs_symlinks=skip_abs_symlinks,
        )

    return plan


def _merge_children(*sources: Path) -> dict[str, list[Path]]:
    """Group the immediate children of several source dirs by name.

    .DS_Store is ignored, matching the link walk.

    Args:
        *sources: Source directories whose children are collected.

    Returns:
        A mapping from child name to the list of paths (one per source) that
        carry an entry with that name.
    """
    by_name: dict[str, list[Path]] = {}
    for s in sources:
        if not s.is_dir():
            continue

        for entry in sorted(s.iterdir()):
            if entry.name == ".DS_Store":
                continue

            by_name.setdefault(entry.name, []).append(entry)

    return by_name


def _all_real_dirs(entries: list[Path]) -> bool:
    """Whether every path is a real directory (a mergeable shared subdir).

    Args:
        entries: Paths to test.

    Returns:
        True if every entry is a directory and none is a symlink.
    """
    return all(e.is_dir() and not e.is_symlink() for e in entries)


def _merge_collisions(dst_dir: Path, *sources: Path) -> list[tuple[str, str]]:
    """Real conflicts from merging `sources` into one directory (read-only).

    Two kegs may share a directory yet hold disjoint entries (the normal case,
    which explodes cleanly). A genuine conflict is a same-named entry that is not
    a directory in every source, a file/file or file/dir clash that cannot be
    merged. Shared subdirectories recurse.

    Args:
        dst_dir: The prefix directory that would receive the merged entries.
        *sources: Keg directories being merged into `dst_dir`.

    Returns:
        List of (destination_path, reason) tuples for every unresolvable conflict,
        or an empty list.
    """
    out: list[tuple[str, str]] = []
    for name, entries in _merge_children(*sources).items():
        if len(entries) < 2:
            continue

        # Same file if resolve to the same real path
        if len({os.path.realpath(e) for e in entries}) == 1:
            continue

        target = dst_dir / name
        if _all_real_dirs(entries):
            out.extend(_merge_collisions(target, *entries))  # Shared subdir

        else:
            out.append((str(target), "provided by multiple kegs"))

    return out


def _merge_into(dst_dir: Path, *sources: Path) -> list[Path]:
    """Link the contents of `sources` into the real directory `dst_dir`.

    Each unique child becomes a relative symlink (whole-dir for directories);
    a directory shared by several sources is itself realised and merged,
    recursively.

    Args:
        dst_dir: The real prefix directory that receives the merged symlinks.
        *sources: Keg directories whose children are linked into `dst_dir`.

    Returns:
        List of absolute prefix paths of every symlink that was created.
    """
    linked: list[Path] = []
    for name, entries in sorted(_merge_children(*sources).items()):
        target = dst_dir / name

        # Same file in multiple kegs; link once
        if len({os.path.realpath(e) for e in entries}) == 1:
            entries = entries[:1]

        if len(entries) > 1 and _all_real_dirs(entries):
            target.mkdir(parents=True, exist_ok=True)
            linked.extend(_merge_into(target, *entries))

        else:
            make_relative_symlink(target, entries[0])
            linked.append(target)

    return linked


def _symlink_dest(link: str) -> str:
    """The directory `link` points at, resolving only `link` itself (one level).

    Args:
        link: The symlink whose destination to resolve.

    Returns:
        The normalised absolute path the symlink targets.
    """
    target = os.readlink(link)
    if os.path.isabs(target):
        return os.path.normpath(target)

    return os.path.normpath(os.path.join(os.path.dirname(link), target))


def _explode(dst: Path, src: Path) -> list[Path]:
    """Replace a whole-dir symlink with a real directory holding both kegs' files.

    Collisions are pre-checked at plan time, so the merge here is conflict-free.

    Args:
        dst: Prefix path that is currently a whole-dir symlink into another keg.
        src: The new keg's matching directory whose contents are merged in.

    Returns:
        List of absolute prefix paths of every symlink created inside the new dir.
    """
    other = _symlink_dest(str(dst))  # Resolve the link only, not the prefix's ancestors
    dst.unlink()  # Drop the whole-dir symlink
    dst.mkdir(parents=True, exist_ok=True)

    return _merge_into(dst, Path(other), src)


def make_relative_symlink(dst: Path, src: Path) -> None:
    """Create a relative symlink, atomically replacing whatever is already at `dst`.

    The link is staged beside `dst` and renamed over it, so a concurrent reader
    never sees `dst` missing, as it would between an unlink and a symlink.

    Args:
        dst: The destination path.
        src: The source path.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Unique per (process, thread)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.unlink(missing_ok=True)  # Stale stage from a crashed peer

    os.symlink(os.path.relpath(src, dst.parent), tmp)
    try:
        os.replace(tmp, dst)

    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _try_symlink(dst: str, src: str) -> bool:
    """Create a relative symlink at `dst` only if nothing occupies it yet.

    The caller guarantees `dst`'s parent exists, so a missing parent surfaces as
    `FileNotFoundError` rather than being silently created.

    Args:
        dst: The destination path.
        src: The source path.

    Returns:
        True if the link was created, False if something is already at `dst`.
    """
    try:
        os.symlink(os.path.relpath(src, os.path.dirname(dst)), dst)

    except FileExistsError:
        return False

    return True


def _describe_existing(dst: str) -> str:
    """The conflict reason for whatever now occupies `dst`, as `consider_link` words it.

    Args:
        dst: The occupied path.

    Returns:
        The symlink's target, or `_EXISTING_FILE` for anything else.

    Raises:
        FileNotFoundError: `dst` was removed again while being inspected.
    """
    if stat.S_ISLNK(os.lstat(dst).st_mode):
        return os.readlink(dst)

    return _EXISTING_FILE


def _link_leaf(
    result: LinkResult, rel: str, dst: str, src: str, *, overwrite: bool
) -> None:
    """Create one leaf symlink, arbitrating a peer that won the race for `dst`.

    The plan saw `dst` absent, but the structure lock excludes only this process,
    so `brew` may have taken the path since. `EEXIST` retakes the decision
    `_Plan.consider_link` made at plan time against the now-current prefix.

    Args:
        result: The result accumulated so far; extended in place.
        rel: The symlink's prefix-relative posix path, as recorded in the result.
        dst: The absolute prefix path of the symlink to create.
        src: The keg path the symlink points at.
        overwrite: Whether to replace a target a peer has taken.

    Raises:
        LinkError: A peer owns `dst` with different contents, and `overwrite` is unset.
    """
    preserve_existing, _ = _walk_opts(rel.split("/", 1)[0])

    for _ in range(_LINK_RACE_RETRIES):
        if _try_symlink(dst, src):
            result.linked.append(rel)
            return

        try:
            existing = _describe_existing(dst)

        except FileNotFoundError:
            continue  # Removed again mid-arbitration; the create is retried

        # Both kegs ship the same real file (e.g. metapackages)
        if os.path.realpath(dst) == os.path.realpath(src):
            result.already_linked.append(rel)
            return

        if preserve_existing and existing == _EXISTING_FILE:
            result.already_linked.append(rel)  # etc: keep the user's file
            return

        if overwrite:
            make_relative_symlink(Path(dst), Path(src))
            result.linked.append(rel)
            return

        raise LinkError([(dst, existing)])

    raise LinkError([(dst, "contended by a concurrent link")])


def _record_link(result: LinkResult, prefix: str, rel: str, src: str) -> None:
    """Create a relative symlink and record it as linked in `result`.

    Args:
        result: The result to extend with the linked prefix path.
        prefix: The prefix the link lives under.
        rel: The prefix-relative posix path of the symlink to create.
        src: The keg path the symlink points at.
    """
    make_relative_symlink(Path(f"{prefix}/{rel}"), Path(src))
    result.linked.append(rel)


def _apply_dirs_and_links(plan: _Plan, result: LinkResult, *, overwrite: bool) -> None:
    """mkpath `plan.dirs` and create `plan.links`, recording both into `result`.

    A plan that touches no shared directory holds no cross-process lock, so each
    leaf is created rather than replaced and arbitrates its own race with `brew`.

    Args:
        plan: The plan whose dirs and leaf-file links to apply.
        result: The result accumulated so far; extended in place.
        overwrite: Whether to replace a target a peer has taken since plan time.
    """
    prefix = plan.prefix
    for rel in plan.dirs:
        os.makedirs(f"{prefix}/{rel}", exist_ok=True)
        result.created_dirs.append(rel)

    # The walk emits links a directory at a time, so the parent is made once per directory
    last_parent = ""
    for rel, src in plan.links:
        dst = f"{prefix}/{rel}"
        parent = dst[: dst.rindex("/")]
        if parent != last_parent:
            os.makedirs(parent, exist_ok=True)
            last_parent = parent

        _link_leaf(result, rel, dst, src, overwrite=overwrite)


def _preview(plan: _Plan) -> LinkResult:
    """Project a plan into the LinkResult applying it would produce.

    A whole-directory symlink counts as one entry here but expands to one entry
    per file if a later keg forces it to explode, so `linked` is a lower bound on
    what a real link would report.

    Args:
        plan: The plan to project.

    Returns:
        A LinkResult describing what applying `plan` would do.
    """
    return LinkResult(
        linked=[rel for rel, _ in (*plan.links, *plan.dir_links, *plan.explosions)],
        created_dirs=list(plan.dirs),
        already_linked=list(plan.already),
        conflicts=list(plan.conflicts),
    )


def _write_linked_record(prefix: Path, name: str, keg: Path) -> None:
    """Write a record of the linked keg.

    Args:
        prefix: The prefix path.
        name: The name of the linked keg.
        keg: The keg path.
    """
    make_relative_symlink(prefix / _LINKED_RECORD_DIR / name, keg)


def _write_link_manifest(keg: Path, result: LinkResult) -> None:
    """Persist the candidate symlink/dir set for fast unlinking.

    Written atomically at the keg root so it shares the keg's lifecycle: removing
    the keg removes the manifest. Unlink realpath-verifies every entry before acting.

    Args:
        keg: The keg directory the links point into.
        result: The result of linking this keg.
    """
    payload = {
        "version": 1,
        "linked": result.linked,
        "created_dirs": result.created_dirs,
    }

    manifest = keg / _LINK_MANIFEST
    tmp = manifest.with_name(manifest.name + ".tmp")
    tmp.write_bytes(orjson.dumps(payload))
    os.replace(tmp, manifest)


def _rollback(keg: Path, prefix: Path, result: LinkResult) -> None:
    """Undo what this call created, after a conflict surfaced part-way through.

    Only entries still resolving into this keg are removed, the same test
    `unlink_keg` applies, so a path a peer has since taken over is ignored.
    An explosion is not reversed, it preserves both kegs' files.

    Args:
        keg: The keg being linked.
        prefix: The prefix being linked into.
        result: The result accumulated so far, naming everything to undo.
    """
    keg_real = Path(os.path.realpath(keg))
    for rel in reversed(result.linked):
        dst = prefix / rel
        if dst.is_symlink() and _points_into(dst, keg_real):
            with contextlib.suppress(OSError):
                dst.unlink()

    _prune_dirs(prefix, set(result.created_dirs))


def link_keg(
    keg_dir: Path,
    *,
    prefix: Path,
    name: str,
    keg_only: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
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
        dry_run: Report what linking would do without touching the filesystem.
            Conflicts are returned on the result rather than raised, so that
            `--overwrite --dry-run` can show what would be deleted.

    Returns:
        A LinkResult describing what was (or would be) linked.
    """
    if keg_only:
        return LinkResult()  # Keg-only formulae are never linked

    result = LinkResult()

    # Planning and applying run under one hold of the in-process structure lock,
    # so a peer thread cannot retype a shared directory in between
    with contextlib.ExitStack() as stack:
        stack.enter_context(_STRUCTURE_LOCK)

        plan = _build_plan(keg_dir, prefix)

        if dry_run:
            return _preview(plan)

        if plan.conflicts and not overwrite:
            raise LinkError(plan.conflicts)

        # Re-validate shared directory ownership under the cross-process lock
        if plan.touches_shared or (overwrite and plan.conflicts):
            stack.enter_context(structure_lock(prefix))

        # A conflict a peer created after the plan was built surfaces mid-apply,
        # so unwind under whatever lock is held to keep the mutate-nothing contract
        try:
            _apply_dirs_and_links(plan, result, overwrite=overwrite)

            if plan.dir_links or plan.explosions or (overwrite and plan.conflicts):
                _apply_shared_dirs(keg_dir, prefix, plan, result, overwrite=overwrite)

        except LinkError:
            _rollback(keg_dir, prefix, result)
            raise

    result.already_linked.extend(plan.already)

    make_relative_symlink(prefix / "opt" / name, keg_dir)

    _write_linked_record(prefix, name, keg_dir)
    _write_link_manifest(keg_dir, result)

    return result


def _reconsider_dir(plan: _Plan, rel: str, src: str) -> None:
    """Re-evaluate one whole-directory target against the current prefix state.

    Mirrors `_walk`'s handling of a single directory entry: if a peer keg has
    since materialised this path as a real directory, descend into it (linking
    the keg's children) instead of treating it as a conflict; if it is a peer's
    whole-dir symlink, explode it; if it is still absent, link the whole dir.

    Args:
        plan: The fresh plan to populate with the re-evaluated ops.
        rel: The target's prefix-relative posix path.
        src: The keg directory being linked there.
    """
    sub = rel.split("/", 1)[0]
    cut = len(sub) + 1
    strategy = _STRATEGIES[sub]
    preserve_existing, skip_abs_symlinks = _walk_opts(sub)

    dst = f"{plan.prefix}/{rel}"
    dst_stat = _lstat(dst)
    action = strategy(rel[cut:], True)

    if action is Action.MKPATH or (
        dst_stat is not None and stat.S_ISDIR(dst_stat.st_mode)
    ):
        if action is Action.MKPATH:
            plan.dirs.append(rel)

        _walk(
            src,
            rel,
            cut,
            strategy,
            plan,
            preserve_existing=preserve_existing,
            skip_abs_symlinks=skip_abs_symlinks,
        )

    else:
        plan.consider_link(
            rel,
            dst,
            src,
            preserve_existing=preserve_existing,
            is_dir=True,
            src_is_symlink=os.path.islink(src),
            dst_stat=dst_stat,
        )


def _apply_shared_dirs(
    keg_dir: Path,
    prefix: Path,
    plan: _Plan,
    result: LinkResult,
    *,
    overwrite: bool,
) -> None:
    """Apply the shared-directory link targets under both structure locks.

    The targets are re-validated first: the plan agrees with what this process
    has done, but `brew` holds neither lock and may have moved a directory since.

    Args:
        keg_dir: The keg being linked.
        prefix: The prefix being linked into.
        plan: The plan whose directory targets are re-validated.
        result: The result accumulated so far; extended in place.
        overwrite: Whether to replace conflicting targets.
    """
    fresh = _Plan(keg=str(keg_dir), prefix=str(prefix))
    seen: set[str] = set()
    for rel, src in (*plan.dir_links, *plan.explosions):
        if rel in seen:
            continue

        seen.add(rel)
        _reconsider_dir(fresh, rel, src)

    if fresh.conflicts and not overwrite:
        raise LinkError(fresh.conflicts)

    _apply_dirs_and_links(fresh, result, overwrite=overwrite)

    for rel, src in fresh.dir_links:
        _record_link(result, fresh.prefix, rel, src)

    for rel, src in fresh.explosions:
        for linked in _explode(Path(f"{fresh.prefix}/{rel}"), Path(src)):
            result.linked.append(linked.relative_to(prefix).as_posix())

    # Under overwrite, replace every conflicting target
    if overwrite:
        cut = len(fresh.prefix) + 1
        for dst, _existing in (*plan.conflicts, *fresh.conflicts):
            # Map the prefix path back to its keg source
            rel = dst[cut:]
            _record_link(result, fresh.prefix, rel, f"{fresh.keg}/{rel}")

    result.already_linked.extend(fresh.already)


def _points_into(link: Path, keg_real: Path) -> bool:
    """Whether a symlink resolves into the given keg.

    Args:
        link: The symlink to test.
        keg_real: The realpath of the keg.

    Returns:
        True if the link's target resolves to the keg or a path within it.
    """
    try:
        real = Path(os.path.realpath(link))

    except OSError:
        return False

    return real == keg_real or keg_real in real.parents


def _iter_symlinks(base: Path):
    """Recursively yield every symlink under base without following symlinked dirs.

    Args:
        base: The directory to scan.

    Yields:
        Each symlink path found (symlinked dirs are yielded, not descended).
    """
    if not base.exists():
        return

    with os.scandir(base) as it:
        for entry in it:
            if entry.is_symlink():
                yield Path(entry.path)

            elif entry.is_dir(follow_symlinks=False):
                yield from _iter_symlinks(Path(entry.path))


def _prune_dirs(prefix: Path, rels: set[str]) -> list[str]:
    """Remove now-empty mkpath'd dirs, deepest first. Eligible roots are kept.

    Args:
        prefix: The prefix directory.
        rels: Relative dir paths to attempt to prune.

    Returns:
        The relative paths that were removed.
    """
    pruned: list[str] = []
    for rel in sorted(rels, key=lambda p: p.count("/"), reverse=True):
        if rel in _ELIGIBLE:  # Never remove shared directories
            continue

        try:
            (prefix / rel).rmdir()  # Succeeds only when empty
            pruned.append(rel)

        except OSError:
            pass  # Still holds another keg's links, or already removed

    return pruned


def unlink_keg(
    keg_dir: Path, *, prefix: Path, name: str, dry_run: bool = False
) -> UnlinkResult:
    """Remove the prefix symlinks pointing into this keg.

    Read the keg's manifest as a candidate set and realpath-verify each entry still
    resolves into this keg before removing it. With no manifest (brew-installed) the
    eligible roots are scanned in full.

    Args:
        keg_dir: The keg being unlinked.
        prefix: The prefix it was linked into.
        name: The formula name (for the linked-keg pointer).
        dry_run: Identify the symlinks without removing them. `pruned` is left
            empty, since which dirs empty out depends on the removals happening.

    Returns:
        An UnlinkResult describing what was (or would be) removed and pruned.
    """
    keg_real = Path(os.path.realpath(keg_dir))
    result = UnlinkResult()

    try:
        manifest = orjson.loads((keg_dir / _LINK_MANIFEST).read_bytes())
        candidates: list[str] = manifest["linked"]
        prune_targets: set[str] = set(manifest.get("created_dirs", []))

    except (OSError, ValueError, KeyError):
        manifest = None
        candidates, prune_targets = [], set()

    # Serialised against concurrent linking, in this process and in peers
    with _STRUCTURE_LOCK, structure_lock(prefix):
        if manifest is not None:
            exploded: list[Path] = []
            for rel in candidates:
                dst = prefix / rel
                if dst.is_symlink():
                    if _points_into(dst, keg_real):
                        if not dry_run:
                            dst.unlink()
                        result.removed.append(rel)

                elif dst.is_dir():
                    exploded.append(dst)  # Explosion: stragglers live under here

            for d in exploded:
                for link in _iter_symlinks(d):
                    if _points_into(link, keg_real):
                        if not dry_run:
                            link.unlink()
                        rel = link.relative_to(prefix).as_posix()
                        result.removed.append(rel)

                prune_targets.add(d.relative_to(prefix).as_posix())

        else:
            result.scanned = True
            for root in _ELIGIBLE:
                for link in _iter_symlinks(prefix / root):
                    if _points_into(link, keg_real):
                        if not dry_run:
                            link.unlink()
                        result.removed.append(link.relative_to(prefix).as_posix())

            prune_targets = {Path(r).parent.as_posix() for r in result.removed}

        if dry_run:
            return result

        result.pruned = _prune_dirs(prefix, prune_targets)

        # Drop the opt link if it still points at this keg
        opt = prefix / "opt" / name
        if opt.is_symlink() and _points_into(opt, keg_real):
            opt.unlink()

        # Drop brew's linked-keg pointer if it still points at this keg
        record = prefix / _LINKED_RECORD_DIR / name
        if record.is_symlink() and _points_into(record, keg_real):
            record.unlink()

    return result
