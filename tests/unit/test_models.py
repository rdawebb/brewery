"""Unit tests for Brewery core data-model helpers and record serialisation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brewery.core.models import InstalledRecord, PackageKind

pytestmark = pytest.mark.unit


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
            installed_on=datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
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
            installed_on=datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        data = InstalledRecord._record_to_cache_dict(record)
        assert data["installed_on"] == "2024-01-02T03:04:05+00:00"

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
            installed_on=datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC),
        )
        restored = InstalledRecord._record_from_cache_dict(
            InstalledRecord._record_to_cache_dict(record)
        )
        assert restored.installed_on == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)

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
