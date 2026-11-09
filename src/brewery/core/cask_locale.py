"""Pick a cask's language variant."""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's `Locale` matching.

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import orjson

from brewery.core.catalog import CaskRow


def config_languages(
    token_dir: Path, parts: Sequence[str] = ("explicit", "env", "default")
) -> list[str]:
    """The languages an installed cask's `config.json` records.

    Args:
        token_dir: The cask's Caskroom directory.
        parts: The config parts to read, in order.

    Returns:
        The languages, empty if there are none or the config is unreadable.
    """
    try:
        config = orjson.loads((token_dir / ".metadata" / "config.json").read_bytes())
        found = [
            lang for part in parts for lang in config.get(part, {}).get("languages", [])
        ]

    except (OSError, ValueError, AttributeError, TypeError):
        return []

    return [lang for lang in dict.fromkeys(found) if isinstance(lang, str)]


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
