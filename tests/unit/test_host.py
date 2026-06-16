"""Unit tests for host platform detection."""

from __future__ import annotations

import pytest

from brewery.core import host as _host_module
from brewery.core.host import Platform, current_platform

pytestmark = pytest.mark.unit


class TestCurrentPlatform:
    """Tests for current_platform, with the platform module monkeypatched."""

    def test_non_darwin_returns_none(self, monkeypatch) -> None:
        """Test that a non-macOS system returns None."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Linux")
        assert current_platform() is None

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
        assert current_platform() == Platform(arch="arm64", macos_major=14)

    def test_resolved_x86_64_platform(self, monkeypatch) -> None:
        """Test that x86_64 normalises to arch='amd64'."""
        monkeypatch.setattr(_host_module._platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            _host_module._platform, "mac_ver", lambda: ("13.6", ("", "", ""), "")
        )
        monkeypatch.setattr(_host_module._platform, "machine", lambda: "x86_64")
        assert current_platform() == Platform(arch="amd64", macos_major=13)
