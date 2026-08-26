"""One definition of the catalog column set, in each shape the tests need."""

from __future__ import annotations

from typing import Any

from brewery.core.catalog import CaskRow, FormulaRow

# Columns stored as 0/1 in sqlite but typed as bool on the row dataclasses
_FORMULA_FLAGS = ("keg_only", "has_service", "post_install", "deprecated", "disabled")
_CASK_FLAGS = ("auto_updates", "deprecated", "disabled")


def formula_dict(name: str, **overrides: Any) -> dict[str, Any]:
    """Build a full-column formula row dict, overridable per field.

    Args:
        name: The name of the formula.
        **overrides: Additional fields to override in the formula row.

    Returns:
        A dictionary representing the full formula row.
    """
    base = {
        "name": name,
        "desc": f"{name} description",
        "homepage": f"https://example/{name}",
        "tap": "homebrew/core",
        "version": "1.0.0",
        "revision": 0,
        "version_scheme": 0,
        "keg_only": 0,
        "has_service": 0,
        "post_install": 0,
        "bottle_url": None,
        "bottle_sha256": None,
        "bottle_cellar": None,
        "bottle_rebuild": 0,
        "deprecated": 0,
        "disabled": 0,
    }
    base.update(overrides)

    return base


def cask_dict(token: str, **overrides: Any) -> dict[str, Any]:
    """Build a full-column cask row dict, overridable per field.

    Args:
        token: The token of the cask.
        **overrides: Additional fields to override in the cask row.

    Returns:
        A dictionary representing the full cask row.
    """
    base = {
        "token": token,
        "name": token.title(),
        "desc": f"{token} description",
        "homepage": f"https://example/{token}",
        "tap": "homebrew/cask",
        "version": "1.0.0",
        "sha256": None,
        "url": None,
        "auto_updates": 0,
        "artifacts": None,
        "depends_on": None,
        "deprecated": 0,
        "disabled": 0,
    }
    base.update(overrides)

    return base


def formula_row(name: str = "wget", **overrides: Any) -> FormulaRow:
    """Build a FormulaRow over the same columns, with merge-friendly defaults.

    Args:
        name: The name of the formula.
        **overrides: Additional fields to override in the row.

    Returns:
        A FormulaRow with the specified fields.
    """
    fields = formula_dict(name, desc="retrieves files", homepage=None, version="1.21.4")
    fields.update(overrides)
    for flag in _FORMULA_FLAGS:
        fields[flag] = bool(fields[flag])

    return FormulaRow(**fields)


def cask_row(token: str = "firefox", **overrides: Any) -> CaskRow:
    """Build a CaskRow over the same columns, with merge-friendly defaults.

    Args:
        token: The token of the cask.
        **overrides: Additional fields to override in the row.

    Returns:
        A CaskRow with the specified fields.
    """
    fields = cask_dict(
        token,
        name=token.title(),
        desc="web browser",
        homepage=None,
        version="120.0",
    )
    fields.update(overrides)
    for flag in _CASK_FLAGS:
        fields[flag] = bool(fields[flag])

    return CaskRow(**fields)
