"""Read-command output parity: the rendered list/search/info/outdated output
reflects the packages, versions, and statuses that the merge derives from a
fixed hermetic layout + fixture catalog.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from brewery.cli.renderers import package_details, package_table
from brewery.core.models import PackageKind

pytestmark = pytest.mark.integration

# Fixed render width so no cell is truncated (short fixture names/versions)
_RENDER_WIDTH = 200


@pytest.fixture
def render(monkeypatch):
    """Render a Rich renderable to plain, color-stripped text at a fixed width.

    Pins renderers._terminal_size so package_table resolves its column widths
    deterministically regardless of the real terminal.

    Args:
        renderable: The Rich renderable to render.

    Returns:
        A function that renders a Rich renderable to plain, color-stripped text.
    """
    from brewery.cli import renderers

    monkeypatch.setattr(
        renderers, "_terminal_size", lambda: (_RENDER_WIDTH, 50), raising=True
    )

    def _render(renderable) -> str:
        """Render a Rich renderable to plain, color-stripped text at a fixed width.

        Args:
            renderable: The Rich renderable to render.

        Returns:
            The rendered text as a string.
        """
        console = Console(
            width=_RENDER_WIDTH, record=True, emoji=False, highlight=False
        )
        console.print(renderable)

        return console.export_text()

    return _render


class TestListOutput:
    """list -> package_table over all installed packages."""

    def test_lists_every_installed_package(self, repo, render) -> None:
        """The three seeded packages (formulae + cask) all appear, with kinds."""
        out = render(package_table(repo.get_all_installed()))

        for name in ("iina", "act", "yazi"):
            assert name in out
        assert "formula" in out and "cask" in out  # kind cells (pkg.kind.value)
        assert "26.5.6" in out  # yazi installed version
        assert "0.2.88" in out  # act installed version

    def test_kind_filter_scopes_to_formulae(self, repo, render) -> None:
        """--kind formula drops the cask from the rendered table."""
        out = render(
            package_table(repo.get_all_installed(kind_filter=PackageKind.FORMULA))
        )

        assert "act" in out and "yazi" in out
        assert "iina" not in out


class TestOutdatedOutput:
    """outdated -> the outdated subset carries latest version and status."""

    def test_outdated_shows_act_with_latest_and_status(self, repo, render) -> None:
        """act (0.2.88 installed, 0.2.89 in catalog) renders as Outdated -> 0.2.89."""
        outdated = repo.get_outdated()
        out = render(package_table(outdated))

        assert "act" in out
        assert "0.2.89" in out  # Catalog latest
        assert "Outdated" in out  # PackageStatus.OUTDATED label
        # yazi is current (26.5.6 == catalog) so it is not in the outdated set
        assert {p.name for p in outdated} >= {"act"}
        assert "yazi" not in out


class TestInfoOutput:
    """info -> package_details for a single package."""

    def test_details_show_name_kind_and_version(self, repo, render) -> None:
        """Detail view of yazi surfaces its name, kind, and installed version."""
        out = render(package_details(repo.get_details("yazi", None)))

        assert "yazi" in out
        assert "formula" in out  # kind cell (pkg.kind.value)
        assert "26.5.6" in out


class TestSearchOutput:
    """search -> package_table over catalog matches."""

    def test_search_surfaces_matching_package(self, repo, render) -> None:
        """Searching a seeded formula name renders it in the results table."""
        out = render(package_table(repo.search("yazi")))

        assert "yazi" in out
