"""Integration tests: provider -> package_builder against real brew JSON.

Verifies the field shapes in the captured fixtures map onto Package objects the
way the rest of the tool expects.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.providers import brew_cask, brew_formula

pytestmark = pytest.mark.integration


def _by_name(pkgs: list[Package], name: str) -> Package:
    """Returns the package with the given name from the list.

    Args:
        pkgs: The list of packages to search.
        name: The name of the package to find.

    Returns:
        The package with the given name.
    """
    return next(p for p in pkgs if p.name == name)


class TestFormulaPipeline:
    """Tests the formula pipeline."""

    async def test_builds_all_installed_formulae(self, mock_brew):
        """Tests that all installed formulae are built correctly."""
        pkgs = await brew_formula.list_installed()
        assert {p.name for p in pkgs} == {"yazi", "act"}
        assert all(p.kind == PackageKind.FORMULA for p in pkgs)

    async def test_yazi_fields(self, mock_brew, fake_env):
        """Tests that the yazi package is built correctly."""
        yazi = _by_name(await brew_formula.list_installed(), "yazi")
        assert yazi.tap == "homebrew/core"
        assert yazi.desc is not None and yazi.desc.startswith("Blazing fast")
        assert yazi.versions == ["26.5.6"]
        assert yazi.metadata["latest_version"] == "26.5.6"
        assert yazi.status == PackageStatus.NONE
        assert yazi.installed_on == datetime.fromtimestamp(1779032846)
        assert yazi.path == str(fake_env.cellar / "yazi" / "26.5.6")
        assert yazi.size_kb == 4096
        assert yazi.deps == []

    async def test_act_is_clean_at_install_time(self, mock_brew, fake_env):
        """Tests that act is clean at install time."""
        act = _by_name(await brew_formula.list_installed(), "act")
        assert act.versions == ["0.2.88"]
        assert act.metadata["latest_version"] == "0.2.88"
        assert act.status == PackageStatus.NONE
        assert act.installed_on == datetime.fromtimestamp(1777809461)
        assert act.path == str(fake_env.cellar / "act" / "0.2.88")


class TestCaskPipeline:
    """Tests the cask pipeline."""

    async def test_builds_single_cask(self, mock_brew):
        """Tests that a single cask is built correctly."""
        pkgs = await brew_cask.list_installed()
        assert len(pkgs) == 1
        assert pkgs[0].name == "iina"
        assert pkgs[0].kind == PackageKind.CASK

    async def test_installed_cask_is_not_flagged_not_linked(self, mock_brew):
        """Tests that an installed cask is not flagged as not linked."""
        iina = (await brew_cask.list_installed())[0]
        assert PackageStatus.NOT_LINKED not in iina.status
        assert iina.status == PackageStatus.NONE

    async def test_versions_include_installed_then_latest(self, mock_brew):
        """Tests that versions include the installed version followed by the latest."""
        iina = (await brew_cask.list_installed())[0]
        assert iina.versions == ["1.4.1,160", "1.4.3"]
        assert iina.metadata["latest_version"] == "1.4.3"

    async def test_metadata_carries_tap(self, mock_brew):
        """Tests that metadata carries the tap."""
        iina = (await brew_cask.list_installed())[0]
        assert iina.metadata["tap"] == "homebrew/cask"

    async def test_path_under_caskroom(self, mock_brew):
        """Tests that path under caskroom is correct."""
        iina = (await brew_cask.list_installed())[0]
        assert iina.path == "/opt/homebrew/Caskroom/iina"

    async def test_installed_on_from_timestamp(self, mock_brew):
        """Tests that installed_on is set from the timestamp."""
        iina = (await brew_cask.list_installed())[0]
        assert iina.installed_on == datetime.fromtimestamp(1770221226.0)
