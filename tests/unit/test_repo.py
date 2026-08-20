"""Unit tests for the Repository facade's public shape."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# Repository is a data facade; a mutating verb landing here is the regression
REPOSITORY_METHODS = {
    "close",
    "get_all_installed",
    "get_details",
    "get_outdated",
    "search",
}


def test_repository_exposes_no_mutating_verbs() -> None:
    """Test that Repository's public surface is still the frozen read-only set.

    Command policy belongs in `brewery.services`; a new public method here means
    the facade is growing back into the god object the split removed.
    """
    from brewery.core.repo import Repository

    public = {
        name
        for name in vars(Repository)
        if not name.startswith("_") and callable(getattr(Repository, name))
    }

    assert public == REPOSITORY_METHODS
