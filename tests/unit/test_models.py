"""Unit tests for Brewery package and dependency models."""

from __future__ import annotations

from datetime import datetime

from brewery.core.models import (
    Dependency,
    Package,
    PackageKind,
    PackageStatus,
    to_serializable,
)


class TestDependency:
    """Tests for Dependency model."""

    def test_from_dict_minimal(self):
        """Test minimal dependency from dict."""
        dep = Dependency.from_dict({"name": "openssl"})
        assert dep.name == "openssl"
        assert dep.optional is False
        assert dep.build is False
        assert dep.test is False

    def test_from_dict_full(self):
        """Test full dependency from dict."""
        dep = Dependency.from_dict(
            {"name": "cmake", "optional": True, "build": True, "test": True}
        )
        assert (dep.optional, dep.build, dep.test) == (True, True, True)


class TestToSerializable:
    """Tests for to_serializable utility."""

    def test_datetime_becomes_isoformat(self):
        """Test datetime is converted to ISO format."""
        dt = datetime(2026, 1, 2, 3, 4, 5)
        assert to_serializable(dt) == dt.isoformat()

    def test_enum_becomes_value(self):
        """Test enum is converted to value."""
        assert to_serializable(PackageKind.FORMULA) == "formula"

    def test_flag_becomes_int_value(self):
        """Test flag is converted to int value."""
        flag = PackageStatus.OUTDATED | PackageStatus.PINNED
        assert to_serializable(flag) == flag.value
        assert isinstance(to_serializable(flag), int)

    def test_nested_structures(self):
        """Test nested structures are serialised recursively."""
        out = to_serializable({"items": [PackageKind.CASK, (1, 2)]})
        assert out == {"items": ["cask", [1, 2]]}

    def test_dataclass_is_recursively_serialised(self):
        """Test dataclass is recursively serialised."""
        dep = Dependency(name="zlib")
        out = to_serializable(dep)
        assert out == {"name": "zlib", "optional": False, "build": False, "test": False}


class TestPackageRoundTrip:
    """Tests for Package round trip."""

    def _sample(self) -> Package:
        """Sample Package instance for testing."""
        return Package(
            name="wget",
            kind=PackageKind.FORMULA,
            versions=["1.21.4"],
            desc="Internet file retriever",
            status=PackageStatus.OUTDATED | PackageStatus.PINNED,
            installed_on=datetime(2026, 1, 15, 9, 30, 0),
            size_kb=2048,
            deps=[Dependency(name="openssl"), Dependency(name="libidn2")],
            used_by=["curl"],
            tap="homebrew/core",
            path="/opt/homebrew/Cellar/wget/1.21.4",
            metadata={"latest_version": "1.22.0"},
        )

    def test_round_trip_preserves_all_fields(self):
        """Test round trip preserves all fields."""
        original = self._sample()
        restored = Package.package_from_dict(original.to_serializable_dict())

        assert restored.name == original.name
        assert restored.kind == original.kind
        assert restored.versions == original.versions
        assert restored.desc == original.desc
        assert restored.status == original.status
        assert restored.installed_on == original.installed_on
        assert restored.size_kb == original.size_kb
        assert [d.name for d in restored.deps] == [d.name for d in original.deps]
        assert restored.used_by == original.used_by
        assert restored.tap == original.tap
        assert restored.path == original.path
        assert restored.metadata == original.metadata

    def test_status_survives_as_flag(self):
        """Test status survives as flag."""
        original = self._sample()
        restored = Package.package_from_dict(original.to_serializable_dict())
        assert isinstance(restored.status, PackageStatus)
        assert PackageStatus.OUTDATED in restored.status
        assert PackageStatus.PINNED in restored.status

    def test_minimal_package_from_dict(self):
        """Test minimal package from dict."""
        pkg = Package.package_from_dict({"name": "jq", "kind": "formula"})
        assert pkg.name == "jq"
        assert pkg.kind == PackageKind.FORMULA
        assert pkg.versions == []
        assert pkg.status == PackageStatus.NONE
        assert pkg.installed_on is None
        assert pkg.deps == []

    def test_no_installed_on_round_trips_to_none(self):
        """Test no installed_on round trips to None."""
        pkg = self._sample()
        pkg.installed_on = None
        restored = Package.package_from_dict(pkg.to_serializable_dict())
        assert restored.installed_on is None
