"""Read-command output parity: the rendered list/search/info/outdated output
reflects the packages, versions, and statuses that the merge derives from a
fixed hermetic layout + fixture catalog.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from brewery.cli.renderers import package_columns, package_details, package_table
from brewery.core.models import PackageKind

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


class TestCompactOutput:
    """list/search -> package_columns brew-style compact view."""

    def test_sections_split_by_kind_with_summary(self, repo, render) -> None:
        """Formulae and Casks get their own sections; act's OUTDATED is summarised."""
        out = render(package_columns(repo.get_all_installed()))

        formulae, _, casks = out.partition("==> Casks")
        assert "==> Formulae" in formulae
        assert "1 outdated" in formulae  # act (0.2.88 -> 0.2.89)
        # Formulae under Formulae, the cask under Casks.
        assert "act" in formulae and "yazi" in formulae
        assert "iina" in casks and "iina" not in formulae

    def test_search_marks_installed_hits(self, repo, render) -> None:
        """An installed search hit carries the green tick."""
        out = render(package_columns(repo.search("yazi"), mark_installed=True))

        assert "yazi ✓" in out

    def test_single_column_one_per_line(self, repo, render) -> None:
        """single_column renders each package on its own line."""
        out = render(
            package_columns(
                repo.get_all_installed(kind_filter=PackageKind.FORMULA),
                single_column=True,
            )
        )

        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        # Header line plus one line per formula (act, yazi), each on its own row.
        assert "==> Formulae" in lines[0]
        assert any(ln.startswith("act") for ln in lines[1:])
        assert any(ln.startswith("yazi") for ln in lines[1:])
        assert len(lines) == 3  # header + act + yazi, no multi-name rows
