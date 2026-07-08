"""Link a Cellar keg's contents into the Homebrew prefix.

Conflict detection is a pre-pass: nothing is mutated if the link would conflict,
so the caller can fall back to `brew link` without a partially linked prefix.
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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import orjson

from brewery.core.errors import LinkError

# Serialises the link operations that mutate ownership of shared prefix directories
_STRUCTURE_LOCK = threading.Lock()

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

# Stored symlink set for fast unlinking
_LINK_MANIFEST = ".brewery_links.json"


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


@dataclass
class UnlinkResult:
    """Result of unlinking a keg from the prefix."""

    removed: list[str] = field(default_factory=list)  # Relative prefix paths unlinked
    pruned: list[str] = field(default_factory=list)  # Emptied dirs removed
    scanned: bool = False  # Fell back to a filesystem scan


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
    links: list[tuple[Path, Path]] = field(default_factory=list)  # (dst, src) files
    dir_links: list[tuple[Path, Path]] = field(
        default_factory=list
    )  # (dst, src) whole-dir symlinks (shared, applied under the lock)
    dirs: list[Path] = field(default_factory=list)
    already: list[Path] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    explosions: list[tuple[Path, Path]] = field(default_factory=list)  # (dst, src)
    # True once the walk links into a shared (non-mkpath) directory
    touches_shared: bool = False

    def consider_link(
        self, dst: Path, src: Path, *, preserve_existing: bool, is_dir: bool
    ) -> None:
        """Consider linking a source file or directory to a destination.

        Args:
            dst: The destination path.
            src: The source path.
            preserve_existing: Whether to preserve existing files.
            is_dir: Whether `src` is a real directory (a whole-directory symlink).
        """
        try:
            dst_stat = os.lstat(dst)

        except (FileNotFoundError, NotADirectoryError):
            is_link = exists = False

        else:
            is_link = stat.S_ISLNK(dst_stat.st_mode)
            exists = True

        # dst resolves to the same real path as src (e.g, metapackages)
        if exists and os.path.realpath(dst) == os.path.realpath(src):
            self.already.append(dst)
            return

        if is_link:
            real_dst = os.path.realpath(dst)
            if is_dir and os.path.isdir(real_dst):
                # Re-link displaced keg's contents, then new keg
                other = Path(real_dst)

                # Pre-check for unsolvable collisions
                collisions = _merge_collisions(dst, other, src)
                if collisions:
                    self.conflicts.extend(collisions)

                else:
                    self.explosions.append((dst, src))
                    self.touches_shared = True

            else:
                self.conflicts.append((str(dst), os.readlink(dst)))

        elif exists:
            if preserve_existing:
                self.already.append(dst)  # etc: keep the user's file

            else:
                self.conflicts.append((str(dst), "an existing file"))

        elif is_dir:
            self.dir_links.append((dst, src))
            self.touches_shared = True

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
    with os.scandir(src_dir) as it:
        entries = sorted(it, key=lambda e: e.name)

    for entry in entries:
        name = entry.name
        if name == ".DS_Store":
            continue

        path = Path(entry.path)
        rel = path.relative_to(sub_root)
        dst = plan.prefix / path.relative_to(plan.keg)
        is_symlink = entry.is_symlink()

        # brew does not link a bin/sbin symlink whose target is absolute
        if skip_abs_symlinks and is_symlink and os.path.isabs(os.readlink(entry.path)):
            continue

        is_dir = entry.is_dir(follow_symlinks=False)

        if not is_dir:
            # brew prunes cached bytecode under site-packages (Python rewrites it)
            if name.endswith(_PYC_EXT) and "/site-packages/" in path.as_posix():
                continue

        elif name.endswith(".app"):
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
            else:
                # Forced descent into a directory a peer keg already exploded into a real dir
                plan.touches_shared = True

            _walk(
                path,
                sub_root,
                strategy,
                plan,
                preserve_existing=preserve_existing,
                skip_abs_symlinks=skip_abs_symlinks,
            )

        else:  # LINK (whole dir or file), or a file under a mkpath dir
            plan.consider_link(
                dst, path, preserve_existing=preserve_existing, is_dir=is_dir
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
    plan = _Plan(keg=keg, prefix=prefix)
    for sub in _ELIGIBLE:
        src = keg / sub
        if not (src.is_dir() and not src.is_symlink()):
            continue

        plan.dirs.append(prefix / sub)  # The eligible root is always a real dir
        preserve_existing, skip_abs_symlinks = _walk_opts(sub)
        _walk(
            src,
            src,
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
            _make_relative_symlink(target, entries[0])
            linked.append(target)

    return linked


def _symlink_dest(link: Path) -> Path:
    """The directory `link` points at, resolving only `link` itself (one level).

    Args:
        link: The symlink whose destination to resolve.

    Returns:
        The normalised absolute path the symlink targets.
    """
    target = os.readlink(link)
    if os.path.isabs(target):
        return Path(target)

    return Path(os.path.normpath(os.path.join(os.path.dirname(link), target)))


def _explode(dst: Path, src: Path) -> list[Path]:
    """Replace a whole-dir symlink with a real directory holding both kegs' files.

    Collisions are pre-checked at plan time, so the merge here is conflict-free.

    Args:
        dst: Prefix path that is currently a whole-dir symlink into another keg.
        src: The new keg's matching directory whose contents are merged in.

    Returns:
        List of absolute prefix paths of every symlink created inside the new dir.
    """
    other = _symlink_dest(dst)  # Resolve the link only, not the prefix's ancestors
    dst.unlink()  # Drop the whole-dir symlink
    dst.mkdir(parents=True, exist_ok=True)

    return _merge_into(dst, other, src)


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


def _record_link(result: LinkResult, prefix: Path, dst: Path, src: Path) -> None:
    """Create a relative symlink and record it as linked in `result`.

    Args:
        result: The result to extend with the linked prefix path.
        prefix: The prefix the link lives under.
        dst: The prefix path of the symlink to create.
        src: The keg path the symlink points at.
    """
    _make_relative_symlink(dst, src)
    result.linked.append(dst.relative_to(prefix).as_posix())


def _apply_dirs_and_links(plan: _Plan, prefix: Path, result: LinkResult) -> None:
    """mkpath `plan.dirs` and create `plan.links`, recording both into `result`.

    Whole-dir symlinks (`plan.dir_links`) are deliberately not applied, they
    are shared targets handled under the structure lock by `_apply_shared_dirs`.

    Args:
        plan: The plan whose dirs and leaf-file links to apply.
        prefix: The prefix being linked into.
        result: The result accumulated so far; extended in place.
    """
    for d in plan.dirs:
        d.mkdir(parents=True, exist_ok=True)
        result.created_dirs.append(d.relative_to(prefix).as_posix())

    for dst, src in plan.links:
        _record_link(result, prefix, dst, src)


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

    # If the plan touches a shared directory or has conflicts, apply under the structure lock
    shared = plan.touches_shared or bool(overwrite and plan.conflicts)
    guard = _STRUCTURE_LOCK if shared else contextlib.nullcontext()
    with guard:
        _apply_dirs_and_links(plan, prefix, result)

        if plan.dir_links or plan.explosions or (overwrite and plan.conflicts):
            _apply_shared_dirs(keg_dir, prefix, plan, result, overwrite=overwrite)

    for dst in plan.already:
        result.already_linked.append(dst.relative_to(prefix).as_posix())

    _write_linked_record(prefix, name, keg_dir)
    _write_link_manifest(keg_dir, result)

    return result


def _reconsider_dir(plan: _Plan, dst: Path, src: Path) -> None:
    """Re-evaluate one whole-directory target against the current prefix state.

    Mirrors `_walk`'s handling of a single directory entry: if a peer keg has
    since materialised this path as a real directory, descend into it (linking
    the keg's children) instead of treating it as a conflict; if it is a peer's
    whole-dir symlink, explode it; if it is still absent, link the whole dir.

    Args:
        plan: The fresh plan to populate with the re-evaluated ops.
        dst: The prefix path of the directory target.
        src: The keg directory being linked there.
    """
    sub = dst.relative_to(plan.prefix).parts[0]
    sub_root = plan.keg / sub
    strategy = _STRATEGIES[sub]
    preserve_existing, skip_abs_symlinks = _walk_opts(sub)

    action = strategy(src.relative_to(sub_root), True)
    descend = action is Action.MKPATH or (dst.is_dir() and not dst.is_symlink())
    if descend:
        if action is Action.MKPATH:
            plan.dirs.append(dst)

        _walk(
            src,
            sub_root,
            strategy,
            plan,
            preserve_existing=preserve_existing,
            skip_abs_symlinks=skip_abs_symlinks,
        )

    else:
        plan.consider_link(dst, src, preserve_existing=preserve_existing, is_dir=True)


def _apply_shared_dirs(
    keg_dir: Path,
    prefix: Path,
    plan: _Plan,
    result: LinkResult,
    *,
    overwrite: bool,
) -> None:
    """Apply the shared-directory link targets under `_STRUCTURE_LOCK`.

    Args:
        keg_dir: The keg being linked.
        prefix: The prefix being linked into.
        plan: The lock-free plan whose directory targets are re-validated.
        result: The result accumulated so far; extended in place.
        overwrite: Whether to replace conflicting targets.
    """
    fresh = _Plan(keg=keg_dir, prefix=prefix)
    seen: set[Path] = set()
    for dst, src in (*plan.dir_links, *plan.explosions):
        if dst in seen:
            continue

        seen.add(dst)
        _reconsider_dir(fresh, dst, src)

    if fresh.conflicts and not overwrite:
        raise LinkError(fresh.conflicts)

    _apply_dirs_and_links(fresh, prefix, result)

    for dst, src in fresh.dir_links:
        _record_link(result, prefix, dst, src)

    for dst, src in fresh.explosions:
        for linked in _explode(dst, src):
            result.linked.append(linked.relative_to(prefix).as_posix())

    # Under overwrite, replace every conflicting target
    if overwrite:
        for dst_str, _existing in (*plan.conflicts, *fresh.conflicts):
            dst = Path(dst_str)

            # Map the prefix path back to its keg source
            src = keg_dir / dst.relative_to(prefix)
            _record_link(result, prefix, dst, src)

    for dst in fresh.already:
        result.already_linked.append(dst.relative_to(prefix).as_posix())


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


def unlink_keg(keg_dir: Path, *, prefix: Path, name: str) -> UnlinkResult:
    """Remove the prefix symlinks pointing into this keg.

    Read the keg's manifest as a candidate set and realpath-verify each entry still
    resolves into this keg before removing it. With no manifest (brew-installed) the
    eligible roots are scanned in full.

    Args:
        keg_dir: The keg being unlinked.
        prefix: The prefix it was linked into.
        name: The formula name (for the linked-keg pointer).

    Returns:
        An UnlinkResult describing what was removed and pruned.
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

    # Serialised against concurrent linking
    with _STRUCTURE_LOCK:
        if manifest is not None:
            exploded: list[Path] = []
            for rel in candidates:
                dst = prefix / rel
                if dst.is_symlink():
                    if _points_into(dst, keg_real):
                        dst.unlink()
                        result.removed.append(rel)

                elif dst.is_dir():
                    exploded.append(dst)  # Explosion: stragglers live under here

            for d in exploded:
                for link in _iter_symlinks(d):
                    if _points_into(link, keg_real):
                        link.unlink()
                        rel = link.relative_to(prefix).as_posix()
                        result.removed.append(rel)

                prune_targets.add(d.relative_to(prefix).as_posix())

        else:
            result.scanned = True
            for root in _ELIGIBLE:
                for link in _iter_symlinks(prefix / root):
                    if _points_into(link, keg_real):
                        link.unlink()
                        result.removed.append(link.relative_to(prefix).as_posix())

            prune_targets = {Path(r).parent.as_posix() for r in result.removed}

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
