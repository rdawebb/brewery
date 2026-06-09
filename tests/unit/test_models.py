"""Unit tests for Brewery core data-model helpers and record serialisation."""

from __future__ import annotations

from datetime import datetime

import pytest

from brewery.core.models import (
    InstalledRecord,
    PackageKind,
    effective_version,
    split_keg_version,
)

pytestmark = pytest.mark.unit


class TestEffectiveVersion:
    """Tests for effective_version."""

    @pytest.mark.parametrize(
        ("version", "revision", "expected"),
        [
            pytest.param("1.2.3", 0, "1.2.3", id="zero_revision_bare_version"),
            pytest.param("1.2.3", None, "1.2.3", id="revision_defaults_to_zero"),
            pytest.param("1.2.3", 4, "1.2.3.4", id="positive_revision_appended"),
            pytest.param("1.2.3", -1, "1.2.3", id="negative_revision_ignored"),
            pytest.param("", 0, "", id="empty_version_no_revision"),
            pytest.param("", 2, ".2", id="empty_version_with_revision"),
        ],
    )
    def test_effective_version(self, version, revision, expected) -> None:
        """Test that effective_version combines version and revision correctly."""
        # revision=None exercises the default-argument path
        if revision is None:
            assert effective_version(version) == expected
        else:
            assert effective_version(version, revision) == expected


class TestSplitKegVersion:
    """Tests for split_keg_version.

    A trailing `_<digits>` splits into (version, revision); anything else yields revision
    zero with the name returned unchanged, and partition uses the first underscore only.
    """

    @pytest.mark.parametrize(
        ("keg", "expected"),
        [
            pytest.param("1.2.3_4", ("1.2.3", 4), id="version_and_revision_split"),
            pytest.param("1.2.3", ("1.2.3", 0), id="no_underscore_zero_revision"),
            pytest.param("1.2.3_beta", ("1.2.3_beta", 0), id="non_digit_tail"),
            pytest.param("1_2_3", ("1_2_3", 0), id="first_underscore_only"),
            pytest.param("1.2.3_", ("1.2.3_", 0), id="trailing_underscore_empty_tail"),
            pytest.param("1.2_3_4", ("1.2_3_4", 0), id="multiple_underscores"),
        ],
    )
    def test_split_keg_version(self, keg, expected) -> None:
        """Test that split_keg_version splits the keg string correctly."""
        assert split_keg_version(keg) == expected


class TestRecordCacheRoundTrip:
    """Tests for InstalledRecord cache (de)serialisation."""

    def _full_record(self) -> InstalledRecord:
        """Build a record with every field set to a non-default value.

        Returns:
            An InstalledRecord with all fields populated.
        """
        return InstalledRecord(
            name="wget",
            kind=PackageKind.FORMULA,
            version="1.21.4",
            revision=2,
            version_scheme=1,
            installed_on=datetime(2024, 1, 2, 3, 4, 5),
            installed_on_request=True,
            installed_as_dependency=True,
            deps=["openssl", "libidn2"],
            head=True,
            tap="homebrew/core",
            path="/opt/homebrew/Cellar/wget/1.21.4_2",
            stale_versions=["1.21.3"],
            linked=True,
            pinned=True,
            used_by=["curl"],
            size_kb=4096,
        )

    def test_full_round_trip_is_lossless(self) -> None:
        """Test that a fully-populated record survives a serialise/deserialise cycle."""
        record = self._full_record()
        restored = InstalledRecord._record_from_cache_dict(
            InstalledRecord._record_to_cache_dict(record)
        )
        assert restored == record

    def test_minimal_round_trip_is_lossless(self) -> None:
        """Test that a minimal record round-trips with defaults intact."""
        record = InstalledRecord(name="jq", kind=PackageKind.CASK, version="1.7")
        restored = InstalledRecord._record_from_cache_dict(
            InstalledRecord._record_to_cache_dict(record)
        )
        assert restored == record

    def test_kind_enum_is_serialised_by_value(self) -> None:
        """Test that kind is stored as its string value, not the enum object."""
        record = InstalledRecord(name="jq", kind=PackageKind.CASK, version="1.7")
        data = InstalledRecord._record_to_cache_dict(record)
        assert data["kind"] == "cask"

    def test_kind_enum_is_restored(self) -> None:
        """Test that a serialised kind value is rebuilt into the enum member."""
        record = InstalledRecord(name="jq", kind=PackageKind.CASK, version="1.7")
        restored = InstalledRecord._record_from_cache_dict(
            InstalledRecord._record_to_cache_dict(record)
        )
        assert restored.kind is PackageKind.CASK

    def test_installed_on_serialised_as_isoformat(self) -> None:
        """Test that a datetime is serialised to an ISO 8601 string."""
        record = InstalledRecord(
            name="jq",
            kind=PackageKind.FORMULA,
            version="1.7",
            installed_on=datetime(2024, 1, 2, 3, 4, 5),
        )
        data = InstalledRecord._record_to_cache_dict(record)
        assert data["installed_on"] == "2024-01-02T03:04:05"

    def test_installed_on_none_serialises_to_none(self) -> None:
        """Test that a missing install date serialises to None, not a string."""
        record = InstalledRecord(name="jq", kind=PackageKind.FORMULA, version="1.7")
        data = InstalledRecord._record_to_cache_dict(record)
        assert data["installed_on"] is None

    def test_installed_on_restored_as_datetime(self) -> None:
        """Test that an ISO date string is rebuilt into a datetime."""
        record = InstalledRecord(
            name="jq",
            kind=PackageKind.FORMULA,
            version="1.7",
            installed_on=datetime(2024, 1, 2, 3, 4, 5),
        )
        restored = InstalledRecord._record_from_cache_dict(
            InstalledRecord._record_to_cache_dict(record)
        )
        assert restored.installed_on == datetime(2024, 1, 2, 3, 4, 5)

    def test_missing_optional_keys_use_defaults(self) -> None:
        """Test that a sparse cache dict (only required keys) restores with defaults."""
        restored = InstalledRecord._record_from_cache_dict(
            {"name": "jq", "kind": "formula", "version": "1.7"}
        )
        assert restored.revision == 0
        assert restored.version_scheme is None
        assert restored.installed_on is None
        assert restored.installed_on_request is False
        assert restored.installed_as_dependency is False
        assert restored.deps == []
        assert restored.head is False
        assert restored.tap is None
        assert restored.path is None
        assert restored.stale_versions == []
        assert restored.linked is False
        assert restored.pinned is False
        assert restored.used_by == []
        assert restored.size_kb is None

    def test_list_fields_are_preserved(self) -> None:
        """Test that list-valued fields round-trip with their contents intact."""
        record = self._full_record()
        restored = InstalledRecord._record_from_cache_dict(
            InstalledRecord._record_to_cache_dict(record)
        )
        assert restored.deps == ["openssl", "libidn2"]
        assert restored.stale_versions == ["1.21.3"]
        assert restored.used_by == ["curl"]
