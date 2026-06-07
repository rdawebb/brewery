"""Unit tests for human-readable status rendering."""

from __future__ import annotations

import pytest

from brewery.cli.renderers import status_to_str
from brewery.core.models import PackageStatus

pytestmark = pytest.mark.unit


class TestStatusToStr:
    """Tests for status_to_str."""

    def test_none_is_up_to_date(self) -> None:
        """Test that NONE renders as the up-to-date label."""
        assert status_to_str(PackageStatus.NONE) == "[green]Up-to-date[/green]"

    def test_single_flag(self) -> None:
        """Test that a single flag renders its label."""
        assert status_to_str(PackageStatus.OUTDATED) == "[red]Outdated[/red]"

    def test_pinned_flag(self) -> None:
        """Test that PINNED renders its label."""
        assert status_to_str(PackageStatus.PINNED) == "[yellow]Pinned[/yellow]"

    def test_keg_only_flag(self) -> None:
        """Test that KEG_ONLY renders its label."""
        assert status_to_str(PackageStatus.KEG_ONLY) == "[magenta]Keg-Only[/magenta]"

    def test_multiple_flags_join_in_label_order(self) -> None:
        """Test that combined flags render in STATUS_LABELS order, comma-joined.

        OUTDATED precedes NOT_LINKED in the label map, so even when combined in
        the other order the output follows the map, not the set/insertion order.
        """
        status = PackageStatus.NOT_LINKED | PackageStatus.OUTDATED
        assert status_to_str(status) == "[red]Outdated[/red], [blue]Not Linked[/blue]"

    def test_head_and_service_order(self) -> None:
        """Test that HEAD precedes Service per the label map ordering."""
        status = PackageStatus.HAS_SERVICE | PackageStatus.HEAD
        assert status_to_str(status) == "[cyan]HEAD[/cyan], [green]Service[/green]"

    def test_all_flags_render_in_order(self) -> None:
        """Test that every flag set renders all labels in map order."""
        status = (
            PackageStatus.OUTDATED
            | PackageStatus.PINNED
            | PackageStatus.NOT_LINKED
            | PackageStatus.KEG_ONLY
            | PackageStatus.HEAD
            | PackageStatus.HAS_SERVICE
        )
        assert status_to_str(status) == (
            "[red]Outdated[/red], "
            "[yellow]Pinned[/yellow], "
            "[blue]Not Linked[/blue], "
            "[magenta]Keg-Only[/magenta], "
            "[cyan]HEAD[/cyan], "
            "[green]Service[/green]"
        )
