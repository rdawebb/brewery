"""Unit tests for human-readable status rendering."""

from __future__ import annotations

import pytest

from brewery.cli.renderers import (
    _compact_entry,
    _section_summary,
    _status_colour,
    status_to_str,
)
from brewery.core.models import Package, PackageKind, PackageStatus

pytestmark = pytest.mark.unit


def _pkg(name: str, status: PackageStatus, *, path: str | None = "/opt/x") -> Package:
    """Build a minimal formula Package for renderer tests."""
    return Package(name=name, kind=PackageKind.FORMULA, status=status, path=path)


class TestStatusToStr:
    """Tests for status_to_str.

    Single flags render their coloured label; combined flags render in
    STATUS_LABELS map order (not set/insertion order), comma-joined.
    """

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param(
                PackageStatus.NONE,
                "[bold green]Up-to-date[/bold green]",
                id="none_up_to_date",
            ),
            pytest.param(
                PackageStatus.OUTDATED, "[bold red]Outdated[/bold red]", id="outdated"
            ),
            pytest.param(
                PackageStatus.PINNED, "[bold yellow]Pinned[/bold yellow]", id="pinned"
            ),
            pytest.param(
                PackageStatus.KEG_ONLY,
                "[bold magenta]Keg-Only[/bold magenta]",
                id="keg_only",
            ),
            pytest.param(
                PackageStatus.NOT_LINKED | PackageStatus.OUTDATED,
                "[bold red]Outdated[/bold red], [bold blue]Not Linked[/bold blue]",
                id="combined_follow_map_order",
            ),
            pytest.param(
                PackageStatus.HAS_SERVICE | PackageStatus.HEAD,
                "[bold cyan]HEAD[/bold cyan], [bold green]Service[/bold green]",
                id="head_before_service",
            ),
            pytest.param(
                PackageStatus.OUTDATED
                | PackageStatus.PINNED
                | PackageStatus.NOT_LINKED
                | PackageStatus.KEG_ONLY
                | PackageStatus.HEAD
                | PackageStatus.HAS_SERVICE,
                "[bold yellow]Pinned[/bold yellow], "
                "[bold red]Outdated[/bold red], "
                "[bold blue]Not Linked[/bold blue], "
                "[bold magenta]Keg-Only[/bold magenta], "
                "[bold cyan]HEAD[/bold cyan], "
                "[bold green]Service[/bold green]",
                id="all_flags_in_map_order",
            ),
        ],
    )
    def test_status_to_str(self, status, expected) -> None:
        """Test the status_to_str function."""
        assert status_to_str(status) == expected


class TestStatusColour:
    """Tests for the compact-view status name colour."""

    @pytest.mark.parametrize(
        ("status", "colour"),
        [
            pytest.param(PackageStatus.PINNED, "bold yellow", id="pinned"),
            pytest.param(PackageStatus.OUTDATED, "bold red", id="outdated"),
            pytest.param(PackageStatus.NOT_LINKED, "bold blue", id="not_linked"),
            pytest.param(
                PackageStatus.PINNED | PackageStatus.OUTDATED,
                "bold yellow",
                id="pinned_wins_over_outdated",
            ),
            pytest.param(
                PackageStatus.OUTDATED | PackageStatus.NOT_LINKED,
                "bold red",
                id="outdated_wins_over_not_linked",
            ),
        ],
    )
    def test_coloured_flags(self, status, colour) -> None:
        """A coloured flag yields the highest-precedence flag's colour."""
        assert _status_colour(_pkg("x", status)) == colour

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param(PackageStatus.NONE, id="up_to_date"),
            pytest.param(PackageStatus.KEG_ONLY, id="keg_only"),
            pytest.param(PackageStatus.HEAD, id="head"),
            pytest.param(PackageStatus.HAS_SERVICE, id="service"),
        ],
    )
    def test_non_coloured_flags(self, status) -> None:
        """Up-to-date and non-actionable states get no colour."""
        assert _status_colour(_pkg("x", status)) is None


class TestSectionSummary:
    """Tests for the section-header count summary."""

    def test_counts_in_display_order_zeros_omitted(self) -> None:
        """Counts read outdated / pinned / not-linked, skipping any zero."""
        pkgs = [
            _pkg("a", PackageStatus.OUTDATED),
            _pkg("b", PackageStatus.OUTDATED | PackageStatus.PINNED),
            _pkg("c", PackageStatus.NONE),
        ]

        # No not-linked package, so that count is omitted entirely.
        assert _section_summary(pkgs).plain == "2 outdated / 1 pinned"

    def test_empty_when_all_up_to_date(self) -> None:
        """A section with no bulleted state summarises to empty."""
        assert _section_summary([_pkg("a", PackageStatus.NONE)]).plain == ""

    def test_counts_coloured_to_match_names(self) -> None:
        """Each count is styled with its status colour; separators stay dim."""
        pkgs = [
            _pkg("a", PackageStatus.OUTDATED),
            _pkg("b", PackageStatus.PINNED),
            _pkg("c", PackageStatus.NOT_LINKED),
        ]

        summary = _section_summary(pkgs)

        # (covered_text, style) for every span, in order.
        spans = [(summary.plain[s.start : s.end], s.style) for s in summary.spans]
        assert spans == [
            ("1 outdated", "bold red"),
            (" / ", "dim"),
            ("1 pinned", "bold yellow"),
            (" / ", "dim"),
            ("1 not linked", "bold blue"),
        ]


class TestCompactEntry:
    """Tests for the name colour and installed tick on compact entries."""

    def test_name_coloured_by_status(self) -> None:
        """The name span carries its status colour, or none when up-to-date."""
        outdated = _compact_entry(
            _pkg("a", PackageStatus.OUTDATED), mark_installed=False
        )
        plain = _compact_entry(_pkg("b", PackageStatus.NONE), mark_installed=False)

        assert (outdated.plain, str(outdated.style)) == ("a", "bold red")
        assert (plain.plain, str(plain.style)) == ("b", "")

    def test_tick_only_when_marking_and_installed(self) -> None:
        """The green tick shows only for installed packages when mark_installed."""
        installed = _compact_entry(
            _pkg("a", PackageStatus.NONE, path="/opt/a"), mark_installed=True
        )
        catalog_only = _compact_entry(
            _pkg("b", PackageStatus.NONE, path=None), mark_installed=True
        )
        unmarked = _compact_entry(
            _pkg("c", PackageStatus.NONE, path="/opt/c"), mark_installed=False
        )

        assert "✓" in installed.plain
        assert "✓" not in catalog_only.plain
        assert "✓" not in unmarked.plain
