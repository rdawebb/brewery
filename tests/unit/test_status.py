"""Unit tests for Brewery package and cask status derivation."""

from __future__ import annotations

from brewery.analysis.status import derive_status
from brewery.core.models import PackageKind, PackageStatus


class TestFormulaStatus:
    """Tests for formula status derivation."""

    def test_empty_info_is_none(self):
        """Test that an empty info dict returns PackageStatus.NONE."""
        assert derive_status({}, kind=PackageKind.FORMULA) == PackageStatus.NONE

    def test_outdated_top_level(self):
        """Test that outdated at the top level sets the OUTDATED flag."""
        status = derive_status({"outdated": True}, kind=PackageKind.FORMULA)
        assert PackageStatus.OUTDATED in status

    def test_outdated_nested_under_version(self):
        """Test that outdated inside the "version" sub-object sets the OUTDATED flag."""
        status = derive_status(
            {"version": {"outdated": True}}, kind=PackageKind.FORMULA
        )
        assert PackageStatus.OUTDATED in status

    def test_pinned_only_when_true(self):
        """Test that pinned only sets the PINNED flag when the value is True."""
        assert PackageStatus.PINNED in derive_status({"pinned": True})

        # A falsy-but-not-True value must not set the flag
        assert PackageStatus.PINNED not in derive_status({"pinned": False})
        assert PackageStatus.PINNED not in derive_status({"pinned": None})

    def test_keg_only(self):
        """Test that keg_only sets the KEG_ONLY flag when the value is True."""
        assert PackageStatus.KEG_ONLY in derive_status({"keg_only": True})

    def test_not_linked_when_installed_and_no_linked_keg(self):
        """Test that not linked when installed and no linked keg sets the NOT_LINKED flag."""
        status = derive_status({"installed": [{"version": "1.0"}], "linked_keg": None})
        assert PackageStatus.NOT_LINKED in status

    def test_linked_keg_present_means_linked(self):
        """Test that linked keg present means linked."""
        status = derive_status({"installed": [{"version": "1.0"}], "linked_keg": "1.0"})
        assert PackageStatus.NOT_LINKED not in status

    def test_not_linked_requires_installed(self):
        """Test that not linked requires installed."""
        # No installed payload => nothing to be unlinked
        status = derive_status({"linked_keg": None})
        assert PackageStatus.NOT_LINKED not in status

    def test_service_dict_sets_has_service(self):
        """Test that service dict sets HAS_SERVICE flag."""
        assert PackageStatus.HAS_SERVICE in derive_status({"service": {"run": "x"}})

    def test_empty_service_dict_does_not_set_flag(self):
        """Test that empty service dict does not set HAS_SERVICE flag."""
        assert PackageStatus.HAS_SERVICE not in derive_status({"service": {}})

    def test_combined_flags(self):
        """Test that combined flags set the correct status."""
        status = derive_status(
            {
                "outdated": True,
                "pinned": True,
                "keg_only": True,
                "installed": [{"version": "1.0"}],
                "linked_keg": None,
            }
        )
        for flag in (
            PackageStatus.OUTDATED,
            PackageStatus.PINNED,
            PackageStatus.KEG_ONLY,
            PackageStatus.NOT_LINKED,
        ):
            assert flag in status

    def test_default_kind_is_formula(self):
        """Test that default kind is formula."""

        # Calling without kind should behave as a formula (applies linked_keg rule)
        status = derive_status({"installed": [{"version": "1.0"}], "linked_keg": None})
        assert PackageStatus.NOT_LINKED in status


class TestCaskStatus:
    """Tests for cask status derivation."""

    def test_installed_cask_is_not_flagged_not_linked(self):
        """Test that installed cask is not flagged as NOT_LINKED."""
        status = derive_status(
            {"installed": "1.2.3", "linked_keg": None}, kind=PackageKind.CASK
        )
        assert PackageStatus.NOT_LINKED not in status

    def test_cask_keg_only_field_ignored(self):
        """Test that keg_only field is ignored for casks."""
        status = derive_status({"keg_only": True}, kind=PackageKind.CASK)
        assert PackageStatus.KEG_ONLY not in status

    def test_cask_outdated(self):
        """Test that outdated field works for casks."""
        status = derive_status({"outdated": True}, kind=PackageKind.CASK)
        assert PackageStatus.OUTDATED in status

    def test_cask_pinned(self):
        """Test that pinned field works for casks."""
        status = derive_status({"pinned": True}, kind=PackageKind.CASK)
        assert PackageStatus.PINNED in status

    def test_clean_installed_cask_is_none(self):
        """Test that clean installed cask is treated as NONE."""
        status = derive_status(
            {"installed": "1.2.3", "linked_keg": None}, kind=PackageKind.CASK
        )
        assert status == PackageStatus.NONE
