"""Unit tests for host platform detection."""

from __future__ import annotations

import pytest

from brewery.core import host as _host_module
from brewery.core.host import Platform, current_platform, preferred_perl_version


class TestCurrentPlatform:
    """Tests for current_platform, with the platform module monkeypatched."""

    @pytest.fixture
    def mock_platform(self, monkeypatch):
        """Pin what the platform module reports about the host.

        Args:
            monkeypatch: The monkeypatch fixture.

        Returns:
            A callable taking the system, machine and macOS version to report.
        """

        def _pin(system: str, machine: str = "", mac_ver: str = "") -> None:
            monkeypatch.setattr(_host_module._platform, "system", lambda: system)
            monkeypatch.setattr(_host_module._platform, "machine", lambda: machine)
            monkeypatch.setattr(
                _host_module._platform, "mac_ver", lambda: (mac_ver, ("", "", ""), "")
            )

        return _pin

    @pytest.mark.parametrize(
        ("system", "machine", "mac_ver", "expected"),
        [
            pytest.param("Windows", "", "", None, id="unknown_system"),
            pytest.param(
                "Linux",
                "x86_64",
                "",
                Platform(arch="amd64", os="linux", macos_major=None),
                id="linux_x86_64",
            ),
            pytest.param(
                "Linux",
                "aarch64",
                "",
                Platform(arch="arm64", os="linux", macos_major=None),
                id="linux_aarch64_normalises",
            ),
            pytest.param("Darwin", "arm64", "", None, id="unresolvable_macos_version"),
            pytest.param("Darwin", "arm64", "x.0", None, id="non_numeric_major"),
            pytest.param(
                "Darwin",
                "arm64",
                "14.5",
                Platform(arch="arm64", os="macos", macos_major=14),
                id="macos_arm64",
            ),
            pytest.param(
                "Darwin",
                "x86_64",
                "13.6",
                Platform(arch="amd64", os="macos", macos_major=13),
                id="macos_x86_64_normalises",
            ),
        ],
    )
    def test_the_host_resolves_to_a_platform(
        self, mock_platform, system, machine, mac_ver, expected
    ) -> None:
        """Test that each host shape resolves to its Platform, or to None."""
        mock_platform(system, machine, mac_ver)

        assert current_platform() == expected


class TestPreferredPerlVersion:
    """Tests for the macOS -> system perl mapping (brew's MacOS.preferred_perl_version)."""

    @pytest.mark.parametrize(
        ("macos_major", "expected"),
        [(15, "5.34"), (14, "5.34"), (13, "5.30"), (11, "5.30"), (10, "5.18")],
    )
    def test_preferred_perl_version_by_macos(self, macos_major, expected) -> None:
        """Tests that each macOS major maps to the expected system perl."""
        assert preferred_perl_version(macos_major) == expected
