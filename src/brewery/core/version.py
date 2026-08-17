"""Homebrew-compatible version ordering.

Versions are compared token by token: a null token equals a numeric zero and outranks
`rc`, and a numeric token always outranks a string.
"""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's version comparison logic.

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache, total_ordering
from typing import ClassVar, TypeVar

_DIGITS = re.compile(r"[0-9]+")

# HEAD or `HEAD-<commit>`
_HEAD_PATTERN = re.compile(r"\AHEAD(?:-.*)?\Z")

# Lazy head plus an optional `_<digits>` revision tail
_PKG_VERSION_PATTERN = re.compile(r"\A(.+?)(?:_(\d+))?\Z")

# Type alias for comparable values (int or str)
_Comparable = TypeVar("_Comparable", int, str)


def is_head(version: str) -> bool:
    """Whether a version string names a HEAD build rather than a release.

    Args:
        version: The version string, such as `HEAD-a1b2c3d`.

    Returns:
        True if the version is `HEAD` or `HEAD-<commit>`.
    """
    return _HEAD_PATTERN.match(version) is not None


def _cmp(left: _Comparable, right: _Comparable) -> int:
    """Three-way comparison of two values.

    Args:
        left: The left-hand value.
        right: The right-hand value.

    Returns:
        -1, 0 or 1 as left sorts before, with, or after right.
    """
    return (left > right) - (left < right)


class _Token:
    """A single component of a version string."""

    __slots__ = ("value",)

    # Whether this token participates in the numeric fast path
    numeric: ClassVar[bool] = False

    def __init__(self, value: str | int | None) -> None:
        """Initialise the token with the parsed value.

        Args:
            value: The token value, already coerced by the subclass.
        """
        self.value = value

    def compare(self, other: _Token) -> int:
        """Three-way comparison against another token.

        Args:
            other: The token to compare against.

        Returns:
            -1, 0 or 1 as this token sorts before, with, or after `other`.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        """Debug representation naming the token class and value.

        Returns:
            A string representation of the token.
        """
        return f"{type(self).__name__}({self.value!r})"


class _NullToken(_Token):
    """The absence of a token, used to pad the shorter side of a comparison."""

    __slots__ = ()

    def __init__(self) -> None:
        """Initialise the null token."""
        super().__init__(None)

    def compare(self, other: _Token) -> int:
        """Compare against another token.

        Args:
            other: The token to compare against.

        Returns:
            -1, 0 or 1. Equal to a numeric zero, greater than the prerelease markers,
            smaller than anything else.
        """
        if isinstance(other, _NullToken):
            return 0

        if isinstance(other, _NumericToken):
            return 0 if other.value == 0 else -1

        if isinstance(other, _AlphaToken | _BetaToken | _PreToken | _RCToken):
            return 1

        return -1


class _StringToken(_Token):
    """A run of letters, compared lexically."""

    __slots__ = ()

    value: str

    def compare(self, other: _Token) -> int:
        """Compare against another token.

        Args:
            other: The token to compare against.

        Returns:
            -1, 0 or 1. Lexical against any other string token (including the composite
            markers), otherwise the inverse of the other token's own rule.
        """
        if isinstance(other, _StringToken):
            return _cmp(self.value, other.value)

        return -other.compare(self)


class _NumericToken(_Token):
    """A run of digits, compared numerically."""

    __slots__ = ()

    numeric: ClassVar[bool] = True

    value: int

    def __init__(self, value: str | int) -> None:
        """Initialise the token as an integer.

        Args:
            value: The matched digits.
        """
        super().__init__(int(value))

    def compare(self, other: _Token) -> int:
        """Compare against another token.

        Args:
            other: The token to compare against.

        Returns:
            -1, 0 or 1. Numeric against another numeric, always greater than a string.
        """
        if isinstance(other, _NumericToken):
            return _cmp(self.value, other.value)

        if isinstance(other, _StringToken):
            return 1

        return -other.compare(self)


class _CompositeToken(_StringToken):
    """A release marker: an alphabetic prefix with an optional numeric suffix."""

    __slots__ = ("rev",)

    # Ordering among the markers; patch and post share a rank
    rank: ClassVar[int]

    def __init__(self, value: str) -> None:
        """Initialise the marker text alongside its numeric suffix.

        Upstream re-scans the suffix on every comparison; a token never changes, so it
        is resolved once here instead.

        Args:
            value: The matched text.
        """
        super().__init__(value)

        match = _DIGITS.search(value)
        self.rev = int(match.group()) if match else 0

    def compare(self, other: _Token) -> int:
        """Compare against another token.

        Args:
            other: The token to compare against.

        Returns:
            -1, 0 or 1. Same marker compares its suffix, different markers compare by
            rank, anything else falls back to the string rules.
        """
        if type(other) is type(self):
            return _cmp(self.rev, other.rev)

        if isinstance(other, _CompositeToken) and self.rank != other.rank:
            return _cmp(self.rank, other.rank)

        return super().compare(other)


class _AlphaToken(_CompositeToken):
    """An alpha prerelease marker."""

    __slots__ = ()

    rank: ClassVar[int] = 0


class _BetaToken(_CompositeToken):
    """A beta prerelease marker."""

    __slots__ = ()

    rank: ClassVar[int] = 1


class _PreToken(_CompositeToken):
    """A generic prerelease marker."""

    __slots__ = ()

    rank: ClassVar[int] = 2


class _RCToken(_CompositeToken):
    """A release-candidate marker."""

    __slots__ = ()

    rank: ClassVar[int] = 3


class _PatchToken(_CompositeToken):
    """A patch-release marker."""

    __slots__ = ()

    rank: ClassVar[int] = 4


class _PostToken(_CompositeToken):
    """A post-release marker."""

    __slots__ = ()

    rank: ClassVar[int] = 4


_NULL_TOKEN = _NullToken()

# The matching group names the token class; order is significant: `pre` ahead of `patch`,
# or `p[0-9]*` claims the bare `p`; Homebrew's `.post[0-9]+` wildcard dot is ported as-is
_TOKEN_KINDS: tuple[tuple[str, str, type[_Token]], ...] = (
    ("alpha", r"alpha[0-9]*|a[0-9]+", _AlphaToken),
    ("beta", r"beta[0-9]*|b[0-9]+", _BetaToken),
    ("pre", r"pre[0-9]*", _PreToken),
    ("rc", r"rc[0-9]*", _RCToken),
    ("patch", r"p[0-9]*", _PatchToken),
    ("post", r".post[0-9]+", _PostToken),
    ("numeric", r"[0-9]+", _NumericToken),
    ("string", r"[a-z]+", _StringToken),
)

_SCAN_PATTERN = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern, _ in _TOKEN_KINDS),
    re.IGNORECASE,
)

_TOKEN_TYPES: dict[str, type[_Token]] = {
    name: token_type for name, _, token_type in _TOKEN_KINDS
}


@lru_cache(maxsize=4096)
def _tokens(version: str) -> tuple[_Token, ...]:
    """Split a version string into its tokens.

    The string fallback is unreachable; it keeps the tokenizer total where upstream
    raises.

    Cached because a merge-on-read pass compares every installed package against the
    catalog, and the same version strings recur across those comparisons.

    Args:
        version: The version string to tokenize.

    Returns:
        The tokens, in order.
    """
    return tuple(
        _TOKEN_TYPES.get(match.lastgroup or "", _StringToken)(match.group())
        for match in _SCAN_PATTERN.finditer(version)
    )


def compare_versions(left: str, right: str) -> int:
    """Compare two upstream version strings the way Homebrew compares them.

    Args:
        left: The left-hand version string.
        right: The right-hand version string.

    Returns:
        -1, 0 or 1 as `left` is older than, the same as, or newer than `right`.
    """
    if left == right:
        return 0

    # A HEAD install outranks every real version
    left_head: bool = is_head(left)
    right_head: bool = is_head(right)

    if left_head or right_head:
        return _cmp(int(left_head), int(right_head))

    left_tokens: tuple[_Token, ...] = _tokens(left)
    right_tokens: tuple[_Token, ...] = _tokens(right)

    # The two sides advance independently: when one holds a numeric token and the other
    # does not, the non-numeric side is skipped rather than compared across
    limit: int = max(len(left_tokens), len(right_tokens))
    lhs = rhs = 0

    while lhs < limit:
        a: _Token = left_tokens[lhs] if lhs < len(left_tokens) else _NULL_TOKEN
        b: _Token = right_tokens[rhs] if rhs < len(right_tokens) else _NULL_TOKEN
        order: int = a.compare(b)

        if order == 0:
            lhs += 1
            rhs += 1

        elif a.numeric and not b.numeric:
            if a.compare(_NULL_TOKEN) > 0:
                return 1

            lhs += 1

        elif not a.numeric and b.numeric:
            if b.compare(_NULL_TOKEN) > 0:
                return -1

            rhs += 1

        else:
            return order

    return 0


@total_ordering
@dataclass(frozen=True, eq=False)
class PkgVersion:
    """An upstream version plus its Homebrew revision.

    The revision is kept separate rather than folded into the version string: it is a
    tiebreak applied only when the upstream versions compare equal.
    """

    version: str
    revision: int = 0

    @classmethod
    def parse(cls, name: str) -> PkgVersion:
        """Split a keg directory name into its version and revision.

        Args:
            name: A keg directory name, such as `1.21.4_2`.

        Returns:
            The parsed version; a name with no trailing `_<digits>` yields revision zero,
            and an empty name yields an empty version.
        """
        match = _PKG_VERSION_PATTERN.match(name)
        if match is None:
            return cls(version=name)

        return cls(version=match.group(1), revision=int(match.group(2) or 0))

    def __str__(self) -> str:
        """The Homebrew rendering: `version` or `version_revision`.

        Returns:
            The string representation of the version, with revision if non-zero.
        """
        return f"{self.version}_{self.revision}" if self.revision > 0 else self.version

    def compare(self, other: PkgVersion) -> int:
        """Three-way comparison against another package version.

        Args:
            other: The version to compare against.

        Returns:
            -1, 0 or 1 as this version is older than, the same as, or newer than `other`.
        """
        result: int = compare_versions(self.version, other.version)

        return result if result != 0 else _cmp(self.revision, other.revision)

    def __eq__(self, other: object) -> bool:
        """Whether two package versions sort equally, e.g. `1.0` and `1.0.0`.

        Args:
            other: The version to compare against.

        Returns:
            True if the versions are equal, False otherwise.
        """
        if not isinstance(other, PkgVersion):
            return NotImplemented

        return self.compare(other) == 0

    def __lt__(self, other: PkgVersion) -> bool:
        """Whether this package version is older than `other`.

        Args:
            other: The version to compare against.

        Returns:
            True if this version is older than `other`, False otherwise.
        """
        if not isinstance(other, PkgVersion):
            return NotImplemented

        return self.compare(other) < 0

    def __hash__(self) -> int:
        """Hash on the raw fields.

        Equality is ordering-based, so two values that hash differently can
        compare equally.

        Returns:
            The hash value of the version.
        """
        return hash((self.version, self.revision))
