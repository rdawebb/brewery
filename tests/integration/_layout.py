"""Shared hermetic Homebrew layout builder and helpers for integration tests."""

from __future__ import annotations

import os
import time
from pathlib import Path

import orjson

from brewery.core.config import BreweryENV


class Brew:
    """Builds a hermetic Homebrew prefix on disk for the scanner to read."""

    def __init__(self, root: Path) -> None:
        """Initialise the Brew layout.

        Args:
            root: The root path for the Brew layout.
        """
        self.prefix = root / "homebrew"
        self.cellar = self.prefix / "Cellar"
        self.caskroom = self.prefix / "Caskroom"
        self.repository = self.prefix / "Homebrew"
        self.cellar.mkdir(parents=True)
        self.caskroom.mkdir(parents=True)

    @property
    def env(self) -> BreweryENV:
        """Get the Brewery environment for the Brew layout.

        Returns:
            The Brewery environment.
        """
        return BreweryENV(
            prefix=self.prefix,
            cellar=self.cellar,
            caskroom=self.caskroom,
            repository=self.repository,
            api_path=self.prefix / "api" / "formula.jws.json",
            bottle_cache=self.prefix / "Cache",
        )

    def formula(
        self,
        name: str,
        version: str,
        *,
        receipt: dict | None = None,
        link_opt: bool = True,
        stale: list[str] | None = None,
    ) -> Path:
        """Create a Cellar keg, optional receipt, optional stale versions, opt link.

        Args:
            name: The name of the formula.
            version: The version of the formula.
            receipt: The installation receipt (if any).
            link_opt: Whether to create an optional symlink.
            stale: A list of stale versions (if any).

        Returns:
            The path to the created keg.
        """
        keg = self.cellar / name / version
        keg.mkdir(parents=True)
        for sv in stale or []:
            (self.cellar / name / sv).mkdir(parents=True)

        if receipt is not None:
            (keg / "INSTALL_RECEIPT.json").write_bytes(orjson.dumps(receipt))

        if link_opt:
            opt_dir = self.prefix / "opt"
            opt_dir.mkdir(exist_ok=True)
            (opt_dir / name).symlink_to(keg)

        return keg

    def cask(self, token: str, versions: list[str]) -> Path:
        """Create a Caskroom token directory with one or more version subdirs.

        Version mtimes are set explicitly (newest last) so 'most recent'
        resolution is deterministic without sleeping between mkdirs.

        Args:
            token: The Caskroom token.
            versions: The list of version subdirectories.

        Returns:
            The path to the created Caskroom directory.
        """
        token_dir = self.caskroom / token
        token_dir.mkdir(parents=True)
        base = time.time()

        for i, v in enumerate(versions):
            d = token_dir / v
            d.mkdir()
            os.utime(d, (base + i, base + i))

        return token_dir

    def link(self, name: str) -> None:
        """Mark a formula linked in brew's bookkeeping directory.

        Args:
            name: The name of the formula.
        """
        d = self.prefix / "var" / "homebrew" / "linked"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).touch()

    def pin(self, name: str) -> None:
        """Mark a formula pinned in brew's bookkeeping directory.

        Args:
            name: The name of the formula.
        """
        d = self.prefix / "var" / "homebrew" / "pinned"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).touch()


def _by_name(records) -> dict:
    """Index records by their name.

    Args:
        records: A list of records with a 'name' attribute.

    Returns:
        A dictionary mapping record names to records.
    """
    return {r.name: r for r in records}


def full_receipt(**overrides) -> dict:
    """A representative INSTALL_RECEIPT.json payload.

    Args:
        overrides: Optional overrides for the receipt fields.

    Returns:
        A dictionary representing the INSTALL_RECEIPT.json payload.
    """
    receipt = {
        "installed_on_request": True,
        "installed_as_dependency": False,
        "time": 1_700_000_000,
        "source": {
            "spec": "stable",
            "tap": "homebrew/core",
            "versions": {"version_scheme": 2},
        },
        "runtime_dependencies": [
            {"full_name": "openssl@3"},
            {"full_name": "ca-certificates"},
        ],
    }
    receipt.update(overrides)

    return receipt
