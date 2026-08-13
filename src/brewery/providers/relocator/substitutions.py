"""Build the placeholder token map for one keg, and apply it to bytes."""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's keg relocation logic.

from __future__ import annotations

import re
from pathlib import Path

from brewery.core.errors import RelocationError
from brewery.core.host import current_platform, preferred_perl_version
from brewery.providers.receipt import RuntimeDependency

_PLACEHOLDER_MARKER = b"@@HOMEBREW_"
_PLACEHOLDER_MARKER_STR = _PLACEHOLDER_MARKER.decode()  # For parsed linkage strings

# Matches brew's Version.formula_optionally_versioned_regex(:openjdk)
_OPENJDK_RE = re.compile(r"\Aopenjdk(@\d+(?:\.\d+)*)?\Z")

# Guards the tab's `preferred_perl` before it is pasted into a shebang path
_PERL_VERSION_RE = re.compile(r"\A\d+\.\d+\Z")

# JAVA_HOME within an openjdk keg: macOS nests it in the .jdk bundle, while
# Linux keeps it directly under libexec (brew's per-OS Keg override)
_MACOS_JAVA_HOME_SUFFIX = "libexec/openjdk.jdk/Contents/Home"
_LINUX_JAVA_HOME_SUFFIX = "libexec"


def build_substitutions(
    prefix: Path,
    cellar: Path,
    repository: Path,
    *,
    extra: dict[str, str] | None = None,
) -> dict[bytes, bytes]:
    """Return the placeholder->value map as bytes (longest token first).

    `extra` carries formula-specific tokens such as `@@HOMEBREW_PERL@@` /
    `@@HOMEBREW_JAVA@@` whose values the pipeline must resolve per formula.

    Args:
        prefix: The Homebrew prefix path.
        cellar: The Homebrew cellar path.
        repository: The Homebrew repository path.
        extra: Additional formula-specific tokens to include.

    Returns:
        A mapping of placeholder bytes to their resolved values.
    """
    subs: dict[bytes, bytes] = {
        b"@@HOMEBREW_PREFIX@@": str(prefix).encode(),
        b"@@HOMEBREW_CELLAR@@": str(cellar).encode(),
        b"@@HOMEBREW_REPOSITORY@@": str(repository).encode(),
        b"@@HOMEBREW_LIBRARY@@": str(repository / "Library").encode(),
    }
    if extra:
        subs.update({k.encode(): v.encode() for k, v in extra.items()})

    # Substitute longer tokens first so no token is a prefix-collision risk
    return dict(sorted(subs.items(), key=lambda kv: len(kv[0]), reverse=True))


def _perl_path(
    prefix: Path, brewed: bool, built_on: dict[str, object] | None, *, is_linux: bool
) -> str:
    """Resolve the `@@HOMEBREW_PERL@@` override path.

    Args:
        prefix: The Homebrew prefix path.
        brewed: Whether the formula depends on the brewed perl.
        built_on: The bottle tab's `built_on` block, if any.
        is_linux: Whether the host is Linux (unversioned system perl).

    Returns:
        The absolute path to the perl interpreter.
    """
    if brewed:
        return str(prefix / "opt" / "perl" / "bin" / "perl")

    if is_linux:
        return "/usr/bin/perl"

    built_version = (built_on or {}).get("preferred_perl")
    if isinstance(built_version, str) and _PERL_VERSION_RE.match(built_version):
        candidate = f"/usr/bin/perl{built_version}"
        if Path(candidate).exists():
            return candidate

    return f"/usr/bin/perl{preferred_perl_version()}"


def formula_tokens(
    prefix: Path,
    *,
    name: str,
    runtime_deps: list[RuntimeDependency],
    built_on: dict[str, object] | None = None,
) -> dict[str, str]:
    """Resolve the formula-specific placeholders for one keg.

    Args:
        prefix: The Homebrew prefix path.
        name: The formula name.
        runtime_deps: The formula's runtime dependency entries.
        built_on: The bottle tab's `built_on` block, if any.

    Returns:
        The formula-specific token map, for `StreamRelocator`'s `extra_tokens`.
    """
    plat = current_platform()
    is_linux = plat is not None and plat.os == "linux"

    brewed_perl = name == "perl" or any(
        d.full_name == "perl" and d.declared_directly for d in runtime_deps
    )
    tokens = {
        "@@HOMEBREW_PERL@@": _perl_path(
            prefix, brewed_perl, built_on, is_linux=is_linux
        )
    }

    openjdk = next(
        (d.full_name for d in runtime_deps if _OPENJDK_RE.match(d.full_name)), None
    )
    if openjdk:
        suffix = _LINUX_JAVA_HOME_SUFFIX if is_linux else _MACOS_JAVA_HOME_SUFFIX
        tokens["@@HOMEBREW_JAVA@@"] = str(prefix / "opt" / openjdk / suffix)

    return tokens


def _reject_unresolved(path: Path, value: bytes) -> None:
    """Raise if a placeholder survived substitution.

    Failing here aborts the native install and lets the caller fall back to brew,
    rather than shipping a broken keg into the Cellar.

    Args:
        path: The file being relocated, for the error message.
        value: The substituted bytes.

    Raises:
        RelocationError: If a placeholder remains.
    """
    start = value.find(_PLACEHOLDER_MARKER)
    if start == -1:
        return

    end = value.find(b"@@", start + len(_PLACEHOLDER_MARKER))
    token = value[start : end + 2] if end != -1 else value[start : start + 40]
    raise RelocationError(
        path, f"unresolved placeholder {token.decode('utf-8', 'replace')}"
    )


def _apply(value: bytes, subs: dict[bytes, bytes]) -> bytes:
    """Apply substitutions to a byte string.

    Args:
        value: The byte string to modify.
        subs: The substitution mapping.

    Returns:
        The modified byte string.
    """
    for token, repl in subs.items():
        if token in value:
            value = value.replace(token, repl)

    return value


def _substitute(path: Path, raw: bytes, subs: dict[bytes, bytes]) -> bytes | None:
    """Substitute placeholders, returning None when nothing changed.

    Args:
        path: The file the bytes came from, for the error message.
        raw: The bytes to substitute.
        subs: A mapping of placeholder bytes to their replacements.

    Returns:
        The substituted bytes, or None if `raw` should be left as it is.

    Raises:
        RelocationError: If a placeholder survived substitution.
    """
    if _PLACEHOLDER_MARKER not in raw:
        return None

    new = _apply(raw, subs)
    _reject_unresolved(path, new)

    return None if new == raw else new
