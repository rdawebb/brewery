"""Unit tests for host platform detection."""

from __future__ import annotations

import pytest

from brewery.core import host as _host_module
from brewery.core.host import Platform, current_platform, preferred_perl_version

pytestmark = pytest.mark.unit


class TestCurrentPlatform:
    """Tests for current_platform, with the platform module monkeypatched."""

    def test_unknown_system_returns_none(self, monkeypatch) -> None:
        """Test that a system that is neither Darwin nor Linux returns None."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Windows")
        assert current_platform() is None

    def test_linux_x86_64_platform(self, monkeypatch) -> None:
        """Test that Linux x86_64 yields os='linux', arch='amd64', no macos_major."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Linux")
        monkeypatch.setattr(_host_module._platform, "machine", lambda: "x86_64")
        assert current_platform() == Platform(
            arch="amd64", os="linux", macos_major=None
        )

    def test_linux_aarch64_platform(self, monkeypatch) -> None:
        """Test that Linux aarch64 normalises to arch='arm64'."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Linux")
        monkeypatch.setattr(_host_module._platform, "machine", lambda: "aarch64")
        assert current_platform() == Platform(
            arch="arm64", os="linux", macos_major=None
        )

    def test_empty_mac_ver_returns_none(self, monkeypatch) -> None:
        """Test that an unresolvable macOS version returns None."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            _host_module._platform, "mac_ver", lambda: ("", ("", "", ""), "")
        )
        assert current_platform() is None

    def test_non_numeric_major_returns_none(self, monkeypatch) -> None:
        """Test that a non-numeric major version returns None."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            _host_module._platform, "mac_ver", lambda: ("x.0", ("", "", ""), "")
        )
        assert current_platform() is None

    def test_resolved_arm64_platform(self, monkeypatch) -> None:
        """Test that arm64 yields a Platform with arch='arm64'."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            _host_module._platform, "mac_ver", lambda: ("14.5", ("", "", ""), "")
        )
        monkeypatch.setattr(_host_module._platform, "machine", lambda: "arm64")
        assert current_platform() == Platform(arch="arm64", os="macos", macos_major=14)

    def test_resolved_x86_64_platform(self, monkeypatch) -> None:
        """Test that x86_64 normalises to arch='amd64'."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            _host_module._platform, "mac_ver", lambda: ("13.6", ("", "", ""), "")
        )
        monkeypatch.setattr(_host_module._platform, "machine", lambda: "x86_64")
        assert current_platform() == Platform(arch="amd64", os="macos", macos_major=13)


class TestPreferredPerlVersion:
    """Tests for the macOS -> system perl mapping (brew's MacOS.preferred_perl_version)."""

    @pytest.mark.parametrize(
        ("macos_major", "expected"),
        [(15, "5.34"), (14, "5.34"), (13, "5.30"), (11, "5.30"), (10, "5.18")],
    )
    def test_preferred_perl_version_by_macos(self, macos_major, expected) -> None:
        """Tests that each macOS major maps to the expected system perl."""
        assert preferred_perl_version(macos_major) == expected
