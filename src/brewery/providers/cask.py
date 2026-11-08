"""Decide which casks brewery can install natively and which go to brew whole."""

from __future__ import annotations

import operator
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from brewery.core.catalog import CaskRow
from brewery.core.host import Platform

Route = Literal["native", "brew"]

# Artifact stanzas brewery can stage itself (`target` annotates an `app`/`binary`)
NATIVE_ARTIFACTS = frozenset(
    {
        "app",
        "binary",
        "font",
        "suite",
        "qlplugin",
        "artifact",
        "manpage",
        "bash_completion",
        "zsh_completion",
        "fish_completion",
        "uninstall",
        "zap",
        "target",
    }
)

# `uninstall`/`zap` directives `cask_directives` runs
NATIVE_UNINSTALL = frozenset(
    {"quit", "delete", "launchctl", "signal", "trash", "rmdir"}
)

_ARCH_TYPES = {"arm": "arm64", "intel": "amd64"}

_COMPARE: dict[str, Callable[[int, int], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}


@dataclass(frozen=True)
class CaskOpts:
    """The parts of `HOMEBREW_CASK_OPTS` a native install has to honour.

    Attributes:
        dirs: `--<name>dir=` overrides, keyed as brew's config keys them.
        binaries: False for `--no-binaries`.
        require_sha: True for `--require-sha`.
        languages: `--language=` values, preferred over the system's.
    """

    dirs: dict[str, Path] = field(default_factory=dict)
    binaries: bool = True
    require_sha: bool = False
    languages: tuple[str, ...] = ()


def read_cask_opts(raw: str) -> CaskOpts:
    """Parse `HOMEBREW_CASK_OPTS` the way brew's `Cask::Config#env` does.

    Args:
        raw: The variable's value.

    Returns:
        The parsed options; unknown switches are ignored.
    """
    dirs: dict[str, Path] = {}
    binaries = True
    languages: tuple[str, ...] = ()
    args = shlex.split(raw)
    for arg in args:
        flag, eq, value = arg.partition("=")
        key = flag.removeprefix("--").replace("-", "_")

        if eq and key == "language":
            languages = tuple(value.split(","))

        elif eq:
            dirs[key] = Path(os.path.abspath(os.path.expanduser(value)))

        elif arg in ("--binaries", "--no-binaries"):
            binaries = arg == "--binaries"  # The last one wins

    return CaskOpts(dirs, binaries, "--require-sha" in args, languages)


def system_languages() -> list[str]:
    """The user's preferred languages.

    Returns:
        The languages in order, empty off macOS or if unset.
    """
    for domain in ("-g", "/Library/Preferences/.GlobalPreferences"):
        try:
            out = subprocess.run(
                ["defaults", "read", domain, "AppleLanguages"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout

        except OSError:
            return []

        if out.strip():
            return re.findall(r'[^ \n"(),]+', out)

    return []


# Locale: language, script and region, each optional, `-` between
_LOCALE = re.compile(
    r"(?:([a-z]{2,3})(?:-(?=.)|$))?(?:([A-Z][a-z]{3})(?:-(?=.)|$))?([A-Z]{2}|\d{3})?",
    re.DOTALL,
)

Locale = tuple[str | None, str | None, str | None]


def parse_locale(text: str) -> Locale | None:
    """Parse a locale.

    Args:
        text: A locale such as `en`, `en-GB` or `zh-Hans-CN`.

    Returns:
        (language, script, region), or None if it isn't a locale.
    """
    match = _LOCALE.fullmatch(text)

    return cast(Locale, match.groups()) if text and match else None


def pick_language(
    variations: Sequence[dict[str, Any]], languages: Sequence[str]
) -> dict[str, Any] | None:
    """Choose a cask's language variant.

    For each language in order, the first variant listing that exact locale wins,
    then the first listing a locale it includes (`en-GB` includes `en`).

    Args:
        variations: The cask's `language_variations`.
        languages: The preferred languages, most preferred first.

    Returns:
        The chosen variant, else the `default` one, else None.
    """
    groups = [
        (v, [loc for s in v["languages"] if (loc := parse_locale(s))])
        for v in variations
    ]
    for language in languages:
        if (want := parse_locale(language)) is None:
            continue

        # A locale includes another whose every part is unset or the same
        variant = next((v for v, locs in groups if want in locs), None) or next(
            (
                v
                for v, locs in groups
                if any(all(p in (None, w) for p, w in zip(loc, want)) for loc in locs)
            ),
            None,
        )
        if variant:
            return variant

    return next((v for v in variations if v.get("default")), None)


# The variant fields a localised row takes over; any other sends the cask to brew
_VARIANT_FIELDS = frozenset({"url", "sha256", "version", "homepage", "artifacts"})


def localise(row: CaskRow, languages: Sequence[str]) -> CaskRow:
    """Apply the cask's chosen language variant over its row.

    Args:
        row: The cask's catalog row.
        languages: The preferred languages, most preferred first.

    Returns:
        The localised row with no `language_variations`, or the row unchanged if
        the variant overrides a field brewery doesn't store.
    """
    if not row.language_variations:
        return row

    variant = pick_language(row.language_variations, languages) or {}
    overrides = {
        k: v for k, v in variant.items() if k not in ("languages", "default", "value")
    }
    if overrides.keys() - _VARIANT_FIELDS:
        return row

    return replace(row, language_variations=None, **overrides)


def classify(row: CaskRow, plat: Platform | None) -> tuple[Route, str]:
    """Route a cask to the native installer or to brew, with the reason.

    Args:
        row: The cask's catalog row.
        plat: The host platform, from `current_platform()`.

    Returns:
        Tuple of (route, reason). The reason is empty for a native cask.
    """
    if plat is None or plat.os != "macos" or plat.macos_major is None:
        return "brew", "native casks need macOS"

    if row.container:
        return "brew", "`container` unpacking"

    if row.url_specs:  # git checkout, headers or POST: only brew's strategies fetch
        return "brew", f"`url {min(row.url_specs)}:`"

    if row.language_variations:
        return "brew", "`language` variant"  # One `localise` couldn't apply

    if reason := _artifact_reason(row.artifacts or [], ("uninstall",)):
        return "brew", reason

    return _check_depends(row.depends_on or {}, plat.macos_major, plat.arch)


def classify_uninstall(
    receipt: dict[str, Any] | None, plat: Platform | None, *, zap: bool
) -> str:
    """Why an installed cask's uninstall goes to brew, judged from its receipt.

    brew uninstalls from the receipt's `uninstall_artifacts`, not the current cask.

    Args:
        receipt: The cask's `INSTALL_RECEIPT.json`, or None if it has none.
        plat: The host platform, from `current_platform()`.
        zap: Whether the `zap` directives run too.

    Returns:
        The reason, or an empty string for a native uninstall.
    """
    if plat is None or plat.os != "macos":
        return "native casks need macOS"

    artifacts = (receipt or {}).get("uninstall_artifacts")
    if not isinstance(artifacts, list) or (receipt or {}).get(
        "uninstall_flight_blocks"
    ):
        return "no uninstall receipt"

    return _artifact_reason(artifacts, ("uninstall", "zap") if zap else ("uninstall",))


def _artifact_reason(artifacts: list[Any], directives: tuple[str, ...]) -> str:
    """The first artifact, or directive of those stanzas, brewery can't handle.

    Args:
        artifacts: The cask's artifacts, or a receipt's `uninstall_artifacts`.
        directives: The stanzas whose directives must be native.

    Returns:
        The reason, or an empty string.
    """
    for stanza in artifacts:
        if not isinstance(stanza, dict):
            return "unrecognised artifact"

        foreign = sorted(stanza.keys() - NATIVE_ARTIFACTS)
        if foreign:
            return f"`{foreign[0]}` artifact"

        for key in directives:
            stanzas = stanza.get(key, [])
            if not isinstance(stanzas, list) or not all(
                isinstance(d, dict) for d in stanzas
            ):
                return f"unrecognised `{key}` directive"

            for directive in stanzas:
                foreign = sorted(directive.keys() - NATIVE_UNINSTALL)
                if foreign:
                    return f"`{key} {foreign[0]}`"

    return ""


def _check_depends(
    depends_on: dict[str, Any], macos_major: int, arch: str
) -> tuple[Route, str]:
    """Hand casks this host can't run to brew, which refuses with its own message.

    `formula` and `cask` dependencies are resolved by the installer.

    Args:
        depends_on: The cask's decoded `depends_on`.
        macos_major: The host's macOS major version.
        arch: The host's arch token.

    Returns:
        Tuple of (route, reason).
    """
    for key in ("macos", "maximum_macos"):
        for op, versions in (depends_on.get(key) or {}).items():
            # The host is known only to its major version, so compare majors
            majors = [int(v.split(".")[0]) for v in versions]
            ok = (
                macos_major in majors
                if op == "=="
                else _COMPARE[op](macos_major, majors[0])
            )
            if not ok:
                return "brew", f"needs macOS {op} {', '.join(versions)}"

    archs = {_ARCH_TYPES.get(a.get("type")) for a in depends_on.get("arch") or []}
    if archs and arch not in archs:
        return "brew", f"needs {'/'.join(sorted(a for a in archs if a))}"

    return "native", ""
