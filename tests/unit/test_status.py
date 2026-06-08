"""Unit tests for the filesystem-knowable half of package status derivation."""

from __future__ import annotations

import pytest

from brewery.analysis.status import derive_local_status
from brewery.core.models import PackageKind, PackageStatus

pytestmark = pytest.mark.unit


class TestFormulaLocalStatus:
    """Tests for locally-derived formula status."""

    def test_clean_linked_formula_is_none(self) -> None:
        """Test that a linked, unpinned, non-HEAD formula has no flags."""
        status = derive_local_status(kind=PackageKind.FORMULA)
        assert status == PackageStatus.NONE

    def test_pinned_sets_flag(self) -> None:
        """Test that a pinned formula sets PINNED."""
        status = derive_local_status(kind=PackageKind.FORMULA, pinned=True)
        assert status == PackageStatus.PINNED

    def test_head_sets_flag(self) -> None:
        """Test that a HEAD build sets HEAD."""
        status = derive_local_status(kind=PackageKind.FORMULA, head=True)
        assert status == PackageStatus.HEAD

    def test_not_linked_sets_flag(self) -> None:
        """Test that an unlinked formula sets NOT_LINKED."""
        status = derive_local_status(kind=PackageKind.FORMULA, linked=False)
        assert status == PackageStatus.NOT_LINKED

    def test_linked_does_not_set_not_linked(self) -> None:
        """Test that a linked formula does not set NOT_LINKED."""
        status = derive_local_status(kind=PackageKind.FORMULA, linked=True)
        assert PackageStatus.NOT_LINKED not in status

    def test_linked_defaults_true(self) -> None:
        """Test that linked defaults to True so a formula is not falsely unlinked.

        Omitting the linked argument must not produce NOT_LINKED.
        """
        status = derive_local_status(kind=PackageKind.FORMULA)
        assert PackageStatus.NOT_LINKED not in status

    def test_all_flags_combine(self) -> None:
        """Test that pinned, HEAD, and unlinked combine into one status."""
        status = derive_local_status(
            kind=PackageKind.FORMULA, head=True, linked=False, pinned=True
        )
        assert PackageStatus.PINNED in status
        assert PackageStatus.HEAD in status
        assert PackageStatus.NOT_LINKED in status

    def test_no_unexpected_flags(self) -> None:
        """Test that only local flags are ever set, never OUTDATED/KEG_ONLY/etc.

        Those are catalog-derived and must not appear in the local half.
        """
        status = derive_local_status(
            kind=PackageKind.FORMULA, head=True, linked=False, pinned=True
        )
        assert PackageStatus.OUTDATED not in status
        assert PackageStatus.KEG_ONLY not in status
        assert PackageStatus.HAS_SERVICE not in status


class TestCaskLocalStatus:
    """Tests that casks carry no locally-derived flags."""

    def test_clean_cask_is_none(self) -> None:
        """Test that a default cask has no flags."""
        assert derive_local_status(kind=PackageKind.CASK) == PackageStatus.NONE

    def test_cask_ignores_pinned(self) -> None:
        """Test that pinned does not apply to casks."""
        status = derive_local_status(kind=PackageKind.CASK, pinned=True)
        assert status == PackageStatus.NONE

    def test_cask_ignores_head(self) -> None:
        """Test that HEAD does not apply to casks."""
        status = derive_local_status(kind=PackageKind.CASK, head=True)
        assert status == PackageStatus.NONE

    def test_cask_ignores_not_linked(self) -> None:
        """Test that an unlinked cask is not flagged NOT_LINKED.

        Linking is a formula concept; casks must never carry NOT_LINKED.
        """
        status = derive_local_status(kind=PackageKind.CASK, linked=False)
        assert status == PackageStatus.NONE

    def test_cask_ignores_all_flags_together(self) -> None:
        """Test that no combination of local flags affects a cask."""
        status = derive_local_status(
            kind=PackageKind.CASK, head=True, linked=False, pinned=True
        )
        assert status == PackageStatus.NONE
