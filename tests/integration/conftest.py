"""Fixtures for Brewery integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

    from brewery.core.catalog import Catalog
    from brewery.core.config import BreweryENV


@pytest.fixture
def mock_env(brew, monkeypatch) -> BreweryENV:
    """Override the base mock_env with the fixed yazi/act/iina layout that
    scan_installed and repo tests need.

    link_opt=False keeps the layout to bare kegs (no opt symlinks, no receipts,
    no link/pin bookkeeping), matching what these cache/repo tests expect.

    Args:
        brew: The fresh Brew layout fixture.
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The mock environment.
    """
    from brewery.core import config

    brew.formula("yazi", "26.5.6", link_opt=False)
    brew.formula("act", "0.2.88", link_opt=False)
    brew.cask("iina", ["1.4.1,160"])

    monkeypatch.setattr(config, "_env_cache", brew.env)

    return brew.env


@pytest.fixture
def empty_catalog(tmp_path) -> Generator[Catalog, None, None]:
    """A fresh Catalog backed by an isolated temp database file.

    Yields the open catalog and closes it on teardown, so tests need no manual
    try/finally close.

    Args:
        tmp_path: The pytest tmp_path fixture.

    Yields:
        The fresh Catalog.
    """
    from brewery.core.catalog import Catalog

    cat = Catalog(db_path=tmp_path / "catalog.db")
    yield cat
    cat.close()
