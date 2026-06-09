"""Unit tests for human-readable status rendering."""

from __future__ import annotations

import pytest

from brewery.cli.renderers import status_to_str
from brewery.core.models import PackageStatus

pytestmark = pytest.mark.unit


class TestStatusToStr:
    """Tests for status_to_str.

    Single flags render their coloured label; combined flags render in
    STATUS_LABELS map order (not set/insertion order), comma-joined.
    """

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param(
                PackageStatus.NONE, "[green]Up-to-date[/green]", id="none_up_to_date"
            ),
            pytest.param(PackageStatus.OUTDATED, "[red]Outdated[/red]", id="outdated"),
            pytest.param(PackageStatus.PINNED, "[yellow]Pinned[/yellow]", id="pinned"),
            pytest.param(
                PackageStatus.KEG_ONLY, "[magenta]Keg-Only[/magenta]", id="keg_only"
            ),
            pytest.param(
                PackageStatus.NOT_LINKED | PackageStatus.OUTDATED,
                "[red]Outdated[/red], [blue]Not Linked[/blue]",
                id="combined_follow_map_order",
            ),
            pytest.param(
                PackageStatus.HAS_SERVICE | PackageStatus.HEAD,
                "[cyan]HEAD[/cyan], [green]Service[/green]",
                id="head_before_service",
            ),
            pytest.param(
                PackageStatus.OUTDATED
                | PackageStatus.PINNED
                | PackageStatus.NOT_LINKED
                | PackageStatus.KEG_ONLY
                | PackageStatus.HEAD
                | PackageStatus.HAS_SERVICE,
                "[red]Outdated[/red], "
                "[yellow]Pinned[/yellow], "
                "[blue]Not Linked[/blue], "
                "[magenta]Keg-Only[/magenta], "
                "[cyan]HEAD[/cyan], "
                "[green]Service[/green]",
                id="all_flags_in_map_order",
            ),
        ],
    )
    def test_status_to_str(self, status, expected) -> None:
        """Test the status_to_str function."""
        assert status_to_str(status) == expected
