"""Unit tests for the filesystem-knowable half of package status derivation."""

from __future__ import annotations

import pytest

from brewery.analysis.status import derive_local_status
from brewery.core.models import PackageKind, PackageStatus

pytestmark = pytest.mark.unit


class TestFormulaLocalStatus:
    """Tests for locally-derived formula status.

    Only local flags (PINNED, HEAD, NOT_LINKED) are ever derivable here; linked
    defaults True so an omitted argument never reads as unlinked.
    """

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            pytest.param({}, PackageStatus.NONE, id="clean_defaults"),
            pytest.param({"linked": True}, PackageStatus.NONE, id="linked_clean"),
            pytest.param({"pinned": True}, PackageStatus.PINNED, id="pinned"),
            pytest.param({"head": True}, PackageStatus.HEAD, id="head"),
            pytest.param({"linked": False}, PackageStatus.NOT_LINKED, id="not_linked"),
            pytest.param(
                {"head": True, "linked": False, "pinned": True},
                PackageStatus.PINNED | PackageStatus.HEAD | PackageStatus.NOT_LINKED,
                id="all_local_flags_combine",
            ),
        ],
    )
    def test_local_status(self, kwargs, expected) -> None:
        """Test local status derivation."""
        assert derive_local_status(kind=PackageKind.FORMULA, **kwargs) == expected

    def test_no_catalog_flags_ever_set(self) -> None:
        """Test that catalog-derived flags never appear in the local half.

        OUTDATED/KEG_ONLY/HAS_SERVICE are catalog-derived and must not leak into
        derive_local_status regardless of the local inputs.
        """
        status = derive_local_status(
            kind=PackageKind.FORMULA, head=True, linked=False, pinned=True
        )
        assert PackageStatus.OUTDATED not in status
        assert PackageStatus.KEG_ONLY not in status
        assert PackageStatus.HAS_SERVICE not in status


class TestCaskLocalStatus:
    """Tests that casks carry no locally-derived flags.

    Linking/pinning/HEAD are formula concepts; no combination of local inputs
    may flag a cask.
    """

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({}, id="clean_defaults"),
            pytest.param({"pinned": True}, id="ignores_pinned"),
            pytest.param({"head": True}, id="ignores_head"),
            pytest.param({"linked": False}, id="ignores_not_linked"),
            pytest.param(
                {"head": True, "linked": False, "pinned": True},
                id="ignores_all_flags",
            ),
        ],
    )
    def test_cask_status_is_always_none(self, kwargs) -> None:
        """Test that cask status is always none."""
        assert (
            derive_local_status(kind=PackageKind.CASK, **kwargs) == PackageStatus.NONE
        )
