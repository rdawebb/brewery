"""Differential tests diffing brewery against the machine's real brew prefix."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from brewery.providers import linker

_CANDIDATES = ["gettext", "python@3.13", "python@3.14", "node", "openssl@3"]


def _symlinks_into(root: Path, keg_real: str) -> set[str]:
    """Collect symlinks under root that resolve into keg_real, descending only real directories.

    Args:
        root: The prefix subdirectory to scan (e.g. `prefix/'bin'`).
        keg_real: The real (resolved) path of the keg as a string; only symlinks
            whose real target starts with this prefix are collected.

    Returns:
        The set of absolute path strings for every matching symlink found.
    """
    found: set[str] = set()
    stack = [str(root)]
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in it:
                if e.is_symlink():
                    if os.path.realpath(e.path).startswith(keg_real):
                        found.add(e.path)

                elif e.is_dir(follow_symlinks=False):
                    stack.append(e.path)

    return found


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("brew") is None,
    reason="requires macOS with Homebrew",
)
def test_plan_matches_brew_links() -> None:
    """Test that the linker plan matches the actual brew links."""
    prefix = Path(
        subprocess.run(
            ["brew", "--prefix"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )

    formula = keg = None
    for cand in _CANDIDATES:
        cellar = prefix / "Cellar" / cand
        if not cellar.is_dir():
            continue
        versions = [p for p in cellar.iterdir() if p.is_dir()]

        # Only useful if it's actually linked (not keg-only / unlinked)
        record = prefix / "var" / "homebrew" / "linked" / cand
        if versions and record.is_symlink():
            formula, keg = cand, versions[-1]
            break

    if keg is None:
        pytest.skip("none of the candidate formulae are installed and linked")

    plan = linker._build_plan(keg, prefix)

    # Against an already-linked keg, every target lands in `already`, not `links`
    brewery_links = {
        f"{prefix}/{rel}"
        for rel in (
            *(rel for rel, _ in plan.links),
            *(rel for rel, _ in plan.dir_links),
            *plan.already,
        )
    }

    # brew's real links into this keg, restricted to the eligible roots.
    keg_real = os.path.realpath(keg)
    brew_links: set[str] = set()
    for sub in linker._ELIGIBLE:
        root = prefix / sub
        if root.is_dir() and not root.is_symlink():
            brew_links |= _symlinks_into(root, keg_real)

    missing = brew_links - brewery_links  # Strategy gap
    spurious = brewery_links - brew_links  # Over-linking
    assert not spurious, (
        f"{formula}: would create links, brew did not: {sorted(spurious)}"
    )
    assert not missing, f"{formula}: strategy gap, brew links missed: {sorted(missing)}"
