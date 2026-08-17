"""Relocate a keg's members as the extractor writes them."""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg relocation logic.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from brewery.core.errors import RelocationError

from .files import (
    _HEAD_BYTES,
    _classify,
    _Kind,
    _relocate_files,
)
from .substitutions import (
    _PLACEHOLDER_MARKER,
    _substitute,
    build_substitutions,
)
from .tools import _codesign

# Largest text member `StreamRelocator` will buffer to substitute in the stream;
# anything bigger falls back to `_process_file`'s own uncapped read
_STREAM_TEXT_CAP = 32 * 1024 * 1024  # 32 MiB


@dataclass(frozen=True)
class RelocationResult:
    """Outcome of relocating a keg.

    `changed_files` is the sorted relative POSIX paths whose text was
    substituted, recorded on the receipt as brew's `changed_files`.
    `macho_relocated` counts inodes, not names - a hard-linked Mach-O is
    patched once and every other name sees it through the shared inode.
    """

    changed_files: list[str]
    macho_relocated: int
    symlinks_relocated: int
    elf_relocated: int = 0


class StreamRelocator:
    """Relocates keg members as the extractor writes them.

    Text members and symlink targets are substituted in the stream; binaries
    (Mach-O, ELF, `ar`) and text over `_STREAM_TEXT_CAP` are deferred to
    `finish`, which hands them to `_process_file` unchanged.

    The extractor calls `member` once per regular file member, then `file` for
    those it answered True to; `link` and `hardlink` come from the deferred
    link pass, and `finish` runs last.
    """

    # What `_classify` needs the extractor to peek
    head_bytes = _HEAD_BYTES

    def __init__(
        self,
        *,
        prefix: Path,
        cellar: Path,
        repository: Path,
        skip_relocation: bool = False,
        extra_tokens: dict[str, str] | None = None,
        text_files: list[str] | None = None,
    ) -> None:
        """Build the substitution map for one keg.

        Args:
            prefix: The new prefix to use.
            cellar: The new cellar path.
            repository: The new repository path.
            skip_relocation: Whether to skip binary dynamic-linkage rewriting.
            extra_tokens: Any extra tokens to use for substitution.
            text_files: The manifest's changed_files list, or None to scan.
        """
        self._subs = build_substitutions(prefix, cellar, repository, extra=extra_tokens)
        self._skip = skip_relocation
        self._text_files = text_files
        self._allowed = None if text_files is None else frozenset(text_files)

        # The <name>/<version> pair, taken from the first member inside the keg
        self._pair: tuple[str, str] | None = None

        self._discovered: list[str] = []
        self._symlinks = 0

        # Deferred members, staging-relative name -> the path it was written to
        self._deferred: dict[str, str] = {}

        # The extra names each deferred path shares an inode with
        self._aliases: dict[str, list[Path]] = {}

        # Hard-link member name -> the member name its inode originates from
        self._alias_root: dict[str, str] = {}

        # Manifest entries the stream actually carried, for `finish`'s check
        self._seen: set[str] = set()

        # (path, keg-relative path) of the member that `file` is about to be handed
        self._pending: tuple[str, str] | None = None

    def _note(self, name: str) -> str | None:
        """Place one member in the keg, marking it off the manifest if listed.

        Every hook starts here: `_locate_keg` enforces a two-component keg root,
        so dropping those two gives the path that `text_files` and `changed_files`
        are written in, with no lookahead needed. The pair is recorded from the
        first member inside the keg, for `finish` to check.

        Args:
            name: The staging-relative member name.

        Returns:
            The keg-relative POSIX path, or None if the member is outside the
            keg (`<name>/.brew/*` included, which `install_to_cellar` never
            clones).
        """
        top, sep, rest = name.partition("/")
        if not sep:
            return None

        version, sep, tail = rest.partition("/")
        if version.startswith("."):
            return None

        if self._pair is None:
            self._pair = (top, version)

        elif (top, version) != self._pair:
            # A second top-level pair; `_locate_keg` rejects the archive anyway
            return None

        rel = tail if sep else ""
        if self._allowed is not None and rel in self._allowed:
            self._seen.add(rel)

        return rel

    def member(self, name: str, path: str, head: bytes, size: int) -> bool:
        """Decide what to do with one regular file member.

        Args:
            name: The staging-relative member name.
            path: The absolute path the member is being written to.
            head: The member's first `head_bytes` bytes, short for a short file.
            size: The member's size in bytes.

        Returns:
            True to buffer the whole body and pass it through `file`, False to
            stream-copy it. A defer has already been recorded either way.
        """
        rel = self._note(name)
        if rel is None:
            return False

        # Too short to hold a placeholder, as `_process_file` decides first too
        if size < len(_PLACEHOLDER_MARKER):
            return False

        kind = _classify(head)

        if kind is _Kind.MACHO or kind is _Kind.ELF:
            # `_process_file` returns early for these under skip_linkage
            if not self._skip:
                self._deferred[name] = path

            return False

        if kind is _Kind.ARCHIVE:
            # Scanning on the way past would need a second copy of
            # `_has_marker`'s seam handling, for a handful of files
            self._deferred[name] = path
            return False

        # In manifest mode, only substitute files brew listed
        if self._allowed is not None and rel not in self._allowed:
            return False

        if size > _STREAM_TEXT_CAP:
            self._deferred[name] = path
            return False

        self._pending = (path, rel)

        return True

    def file(self, data: bytes) -> bytes:
        """Substitute the buffered body of the member `member` just accepted.

        Called once per True from `member` and before the next one, which is
        what lets the path and relative name be stashed rather than passed back.

        Args:
            data: The member's whole body.

        Returns:
            The bytes to write, `data` itself when nothing matched.

        Raises:
            RelocationError: If a placeholder survived substitution.
        """
        # The one-`file`-per-accepting-`member` contract, which `_Sink` has no
        # way to express, so it is asserted rather than typed
        assert self._pending is not None, "file() without an accepting member()"
        path, rel = self._pending
        self._pending = None

        new = _substitute(Path(path), data, self._subs)
        if new is None:
            return data

        self._discovered.append(rel)

        return new

    def link(self, name: str, path: str, target: str) -> str:
        """Substitute a symlink's target before the link is created.

        Args:
            name: The staging-relative member name.
            path: The absolute path of the link, for error context.
            target: The link target as the archive recorded it.

        Returns:
            The target to create the link with.

        Raises:
            RelocationError: If a placeholder survived substitution.
        """
        # `_note` marks a tab entry naming a symlink off the manifest, which
        # brew's tabs do carry, so `finish` does not report it as missing
        if self._note(name) is None:
            return target

        raw = target.encode("utf-8", "surrogateescape")
        new = _substitute(Path(path), raw, self._subs)
        if new is None:
            return target

        self._symlinks += 1

        return new.decode("utf-8", "surrogateescape")

    def hardlink(self, name: str, path: str, target: str) -> None:
        """Record a hard-link member.

        The target is a member name, not a path, so there is nothing to
        substitute: patching the inode under the deferred name covers every
        other name.

        `codesign` replaces the file rather than rewriting it, leaving the
        other names on the old inode, so aliases of a deferred member are
        tracked for `finish`'s batch.

        Args:
            name: The staging-relative member name.
            path: The absolute path of the link.
            target: The member name whose inode this link shares.
        """
        self._note(name)

        # A link to a link still resolves to the inode the first name wrote
        root = self._alias_root.get(target, target)
        self._alias_root[name] = root

        deferred = self._deferred.get(root)
        if deferred is not None:
            self._aliases.setdefault(deferred, []).append(Path(path))

    def defer(self, name: str, path: str) -> None:
        """Relocate a member in `finish` rather than in the stream.

        For members the extractor cannot show a contiguous head of, such as a
        sparse body; `_process_file` classifies the finished file instead.

        Args:
            name: The staging-relative member name.
            path: The absolute path the member was written to.
        """
        if self._note(name) is None:
            return

        self._deferred[name] = path

    def finish(self, keg_dir: Path) -> RelocationResult:
        """Relocate the deferred binaries and report the whole keg's outcome.

        Args:
            keg_dir: The keg directory `extract_bottle` resolved.

        Returns:
            The keg's RelocationResult, merging what the stream substituted with
            what the deferred post-pass rewrote.

        Raises:
            RelocationError: If the keg root is not the one the stream inferred,
                if a listed text file never arrived, or if a file could not be
                relocated.
        """
        found = (keg_dir.parent.name, keg_dir.name)
        if self._pair is not None and self._pair != found:
            raise RelocationError(
                keg_dir,
                f"stream saw keg root {'/'.join(self._pair)!r}, not {'/'.join(found)!r}",
            )

        if self._text_files is not None:
            # Check that all listed text files are present in the keg
            for rel in self._text_files:
                if rel not in self._seen:
                    raise RelocationError(
                        keg_dir / rel, "manifest changed_files entry missing from keg"
                    )

        to_sign, discovered, elf_n = _relocate_files(
            list(self._deferred.values()),
            self._subs,
            str(keg_dir),
            self._allowed,
            self._skip,
        )

        # `to_sign` may contain aliases, so collect them all up front
        batch = list(to_sign)
        for path in to_sign:
            batch.extend(self._aliases.get(str(path), ()))

        _codesign(batch)

        # The manifest list is authoritative for the receipt
        changed = (
            sorted(self._text_files)
            if self._text_files is not None
            else sorted(self._discovered + discovered)
        )

        return RelocationResult(changed, len(to_sign), self._symlinks, elf_n)
