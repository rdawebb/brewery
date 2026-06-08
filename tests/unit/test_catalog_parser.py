"""Unit tests for catalog JSON parsing and platform/bottle resolution."""

from __future__ import annotations

import orjson
import pytest

from brewery.core import catalog_parser
from brewery.core.catalog_parser import (
    Bottle,
    Platform,
    _json_text,
    _macos_tag,
    _parse_cask,
    _parse_formula,
    candidate_tags,
    current_platform,
    platform_tag,
    resolve_bottle,
)

pytestmark = pytest.mark.unit


class TestMacosTag:
    """Tests for _macos_tag."""

    def test_arm64_prefixes_codename(self) -> None:
        """Test that Apple Silicon prefixes the codename with arm64_."""
        assert _macos_tag(arch="arm64", codename="sonoma") == "arm64_sonoma"

    def test_intel_uses_bare_codename(self) -> None:
        """Test that Intel uses the bare codename."""
        assert _macos_tag(arch="x86_64", codename="sonoma") == "sonoma"

    def test_unknown_arch_treated_as_intel(self) -> None:
        """Test that any non-arm64 arch falls into the bare-codename branch."""
        assert _macos_tag(arch="ppc", codename="sonoma") == "sonoma"


class TestCandidateTags:
    """Tests for candidate_tags."""

    def test_descending_order_and_any_fallback(self) -> None:
        """Test that tags descend from the current major and end with the any tag."""
        tags = candidate_tags(Platform(arch="arm64", macos_major=15))
        assert tags == [
            "arm64_sequoia",
            "arm64_sonoma",
            "arm64_ventura",
            "arm64_monterey",
            "arm64_big_sur",
            "all",
        ]

    def test_excludes_majors_newer_than_current(self) -> None:
        """Test that codenames newer than the running major are excluded."""
        tags = candidate_tags(Platform(arch="x86_64", macos_major=13))
        assert tags == ["ventura", "monterey", "big_sur", "all"]
        assert "sonoma" not in tags
        assert "tahoe" not in tags

    def test_current_major_included(self) -> None:
        """Test that the running major's own codename is the first tag."""
        tags = candidate_tags(Platform(arch="arm64", macos_major=14))
        assert tags[0] == "arm64_sonoma"

    def test_any_tag_always_last(self) -> None:
        """Test that the arch-independent tag is always appended last."""
        tags = candidate_tags(Platform(arch="arm64", macos_major=11))
        assert tags[-1] == "all"

    def test_major_below_known_range_yields_only_any(self) -> None:
        """Test that a major below every known codename yields only the any tag."""
        tags = candidate_tags(Platform(arch="arm64", macos_major=10))
        assert tags == ["all"]


class TestPlatformTag:
    """Tests for platform_tag."""

    def test_known_major(self) -> None:
        """Test that a known major maps to its codename tag."""
        assert platform_tag(Platform(arch="arm64", macos_major=14)) == "arm64_sonoma"

    def test_unknown_major_stringifies_number(self) -> None:
        """Test that an unknown major falls back to the stringified number."""
        assert platform_tag(Platform(arch="arm64", macos_major=99)) == "arm64_99"

    def test_intel_known_major(self) -> None:
        """Test that an Intel known major uses the bare codename."""
        assert platform_tag(Platform(arch="x86_64", macos_major=13)) == "ventura"


class TestCurrentPlatform:
    """Tests for current_platform, with the platform module monkeypatched."""

    def test_non_darwin_returns_none(self, monkeypatch) -> None:
        """Test that a non-macOS system returns None."""
        monkeypatch.setattr(catalog_parser._platform, "system", lambda: "Linux")
        assert current_platform() is None

    def test_empty_mac_ver_returns_none(self, monkeypatch) -> None:
        """Test that an unresolvable macOS version returns None."""
        monkeypatch.setattr(catalog_parser._platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            catalog_parser._platform, "mac_ver", lambda: ("", ("", "", ""), "")
        )
        assert current_platform() is None

    def test_non_numeric_major_returns_none(self, monkeypatch) -> None:
        """Test that a non-numeric major version returns None."""
        monkeypatch.setattr(catalog_parser._platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            catalog_parser._platform, "mac_ver", lambda: ("x.0", ("", "", ""), "")
        )
        assert current_platform() is None

    def test_resolved_platform(self, monkeypatch) -> None:
        """Test that a valid macOS version yields a Platform with the major."""
        monkeypatch.setattr(catalog_parser._platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            catalog_parser._platform, "mac_ver", lambda: ("14.5", ("", "", ""), "")
        )
        monkeypatch.setattr(catalog_parser._platform, "machine", lambda: "arm64")
        plat = current_platform()
        assert plat == Platform(arch="arm64", macos_major=14)


class TestResolveBottle:
    """Tests for resolve_bottle."""

    def _files(self) -> dict:
        return {
            "arm64_sonoma": {
                "url": "https://example/arm64_sonoma",
                "sha256": "aaa",
                "cellar": ":any",
            },
            "ventura": {
                "url": "https://example/ventura",
                "sha256": "bbb",
                "cellar": "/usr/local",
            },
            "all": {
                "url": "https://example/all",
                "sha256": "ccc",
                "cellar": ":any_skip_relocation",
            },
        }

    def test_no_files_returns_none(self) -> None:
        """Test that an empty files map returns None."""
        assert resolve_bottle(files={}, platform=Platform("arm64", 14)) is None

    def test_no_platform_returns_none(self) -> None:
        """Test that a None platform (source-only) returns None."""
        assert resolve_bottle(files=self._files(), platform=None) is None

    def test_picks_first_matching_candidate(self) -> None:
        """Test that the highest-preference matching tag is chosen.

        On arm64 Sonoma the exact arm64_sonoma bottle wins over the all tag.
        """
        bottle = resolve_bottle(files=self._files(), platform=Platform("arm64", 14))
        assert bottle == Bottle(
            url="https://example/arm64_sonoma", sha256="aaa", cellar=":any"
        )

    def test_falls_back_to_any_tag(self, monkeypatch) -> None:
        """Test that the arch-independent tag is used when no arch tag matches."""
        files = {
            "all": {"url": "u", "sha256": "s", "cellar": ":any"},
        }
        bottle = resolve_bottle(files=files, platform=Platform("arm64", 14))
        assert bottle == Bottle(url="u", sha256="s", cellar=":any")

    def test_no_matching_tag_returns_none(self) -> None:
        """Test that a files map with no candidate tag returns None."""
        files = {"linux": {"url": "u", "sha256": "s", "cellar": ":any"}}
        assert resolve_bottle(files=files, platform=Platform("arm64", 14)) is None

    def test_empty_entry_is_skipped(self) -> None:
        """Test that a falsy (empty-dict) entry is skipped, not selected.

        files.get(tag) is truthiness-checked, so an empty dict under the only
        candidate tag yields no match rather than an all-None Bottle.
        """
        files = {"all": {}}
        assert resolve_bottle(files=files, platform=Platform("arm64", 14)) is None

    def test_partial_entry_fills_missing_with_none(self) -> None:
        """Test that a truthy entry missing some keys maps them to None."""
        files = {"all": {"url": "u"}}
        bottle = resolve_bottle(files=files, platform=Platform("arm64", 14))
        assert bottle == Bottle(url="u", sha256=None, cellar=None)


class TestParseFormula:
    """Tests for _parse_formula."""

    def _obj(self, **overrides) -> dict:
        obj = {
            "name": "wget",
            "desc": "retrieves files",
            "homepage": "https://example",
            "tap": "homebrew/core",
            "versions": {"stable": "1.21.4"},
            "revision": 2,
            "version_scheme": 1,
            "keg_only": True,
            "service": {"run": "wget"},
            "post_install_defined": True,
            "deprecated": True,
            "disabled": False,
            "dependencies": ["openssl", "libidn2"],
            "aliases": ["wngt"],
            "oldnames": ["wget2"],
            "bottle": {
                "stable": {
                    "rebuild": 3,
                    "files": {"all": {"url": "u", "sha256": "s", "cellar": ":any"}},
                }
            },
        }
        obj.update(overrides)
        return obj

    def test_row_field_mapping(self) -> None:
        """Test that scalar fields map onto the catalog row."""
        row, _, _ = _parse_formula(self._obj(), platform=Platform("arm64", 14))
        assert row["name"] == "wget"
        assert row["desc"] == "retrieves files"
        assert row["version"] == "1.21.4"
        assert row["revision"] == 2
        assert row["version_scheme"] == 1

    def test_boolean_coercion(self) -> None:
        """Test that truthy source values are coerced to real bools."""
        row, _, _ = _parse_formula(self._obj(), platform=Platform("arm64", 14))
        assert row["keg_only"] is True
        assert row["has_service"] is True  # derived from non-empty service dict
        assert row["post_install"] is True
        assert row["deprecated"] is True
        assert row["disabled"] is False

    def test_has_service_false_when_absent(self) -> None:
        """Test that has_service is False when no service block is present."""
        row, _, _ = _parse_formula(
            self._obj(service=None), platform=Platform("arm64", 14)
        )
        assert row["has_service"] is False

    def test_missing_stable_version_becomes_empty(self) -> None:
        """Test that a missing stable version defaults to the empty string."""
        row, _, _ = _parse_formula(
            self._obj(versions={}), platform=Platform("arm64", 14)
        )
        assert row["version"] == ""

    def test_bottle_resolved_into_row(self) -> None:
        """Test that a resolved bottle's fields populate the row."""
        row, _, _ = _parse_formula(self._obj(), platform=Platform("arm64", 14))
        assert row["bottle_url"] == "u"
        assert row["bottle_sha256"] == "s"
        assert row["bottle_cellar"] == ":any"
        assert row["bottle_rebuild"] == 3

    def test_no_platform_yields_no_bottle(self) -> None:
        """Test that a None platform leaves bottle fields unset."""
        row, _, _ = _parse_formula(self._obj(), platform=None)
        assert row["bottle_url"] is None
        assert row["bottle_sha256"] is None
        assert row["bottle_cellar"] is None

    def test_deps_are_runtime_rows(self) -> None:
        """Test that dependencies become runtime dep rows keyed by package."""
        _, deps, _ = _parse_formula(self._obj(), platform=Platform("arm64", 14))
        assert deps == [
            {"pkg": "wget", "dep": "openssl", "kind": "runtime"},
            {"pkg": "wget", "dep": "libidn2", "kind": "runtime"},
        ]

    def test_aliases_and_oldnames_both_resolve_to_canonical(self) -> None:
        """Test that aliases and oldnames both map to the canonical name."""
        _, _, aliases = _parse_formula(self._obj(), platform=Platform("arm64", 14))
        assert aliases == [
            {"alias": "wngt", "name": "wget"},
            {"alias": "wget2", "name": "wget"},
        ]

    def test_missing_collections_default_empty(self) -> None:
        """Test that absent deps/aliases/oldnames produce empty lists."""
        obj = {"name": "bare", "versions": {"stable": "1.0"}}
        row, deps, aliases = _parse_formula(obj, platform=None)
        assert deps == []
        assert aliases == []
        assert row["revision"] == 0
        assert row["version_scheme"] == 0


class TestParseCask:
    """Tests for _parse_cask."""

    def _obj(self, **overrides) -> dict:
        obj = {
            "token": "firefox",
            "name": ["Firefox", "Firefox Browser"],
            "desc": "web browser",
            "homepage": "https://example",
            "tap": "homebrew/cask",
            "version": "120.0",
            "sha256": "abc",
            "url": "https://example/dmg",
            "auto_updates": True,
            "artifacts": [{"app": "Firefox.app"}],
            "depends_on": {"macos": ">= 11"},
            "deprecated": False,
            "disabled": False,
        }
        obj.update(overrides)
        return obj

    def test_display_name_is_first_of_list(self) -> None:
        """Test that the display name is the first entry of the name list."""
        row = _parse_cask(self._obj())
        assert row["name"] == "Firefox"

    def test_empty_name_list_yields_none(self) -> None:
        """Test that an empty name list resolves the display name to None."""
        row = _parse_cask(self._obj(name=[]))
        assert row["name"] is None

    def test_non_list_name_yields_none(self) -> None:
        """Test that a non-list name field resolves to None."""
        row = _parse_cask(self._obj(name="Firefox"))
        assert row["name"] is None

    def test_scalar_field_mapping(self) -> None:
        """Test that scalar cask fields map onto the row."""
        row = _parse_cask(self._obj())
        assert row["token"] == "firefox"
        assert row["version"] == "120.0"
        assert row["sha256"] == "abc"
        assert row["auto_updates"] is True

    def test_artifacts_and_depends_on_are_json_text(self) -> None:
        """Test that nested artifacts/depends_on are encoded as JSON text."""
        row = _parse_cask(self._obj())
        assert orjson.loads(row["artifacts"]) == [{"app": "Firefox.app"}]
        assert orjson.loads(row["depends_on"]) == {"macos": ">= 11"}

    def test_absent_nested_fields_become_none(self) -> None:
        """Test that absent artifacts/depends_on encode to None."""
        row = _parse_cask(self._obj(artifacts=None, depends_on=None))
        assert row["artifacts"] is None
        assert row["depends_on"] is None


class TestJsonText:
    """Tests for _json_text."""

    def test_none_for_empty_values(self) -> None:
        """Test that empty/falsy values encode to None."""
        assert _json_text(None) is None
        assert _json_text([]) is None
        assert _json_text({}) is None
        assert _json_text("") is None

    def test_encodes_dict(self) -> None:
        """Test that a non-empty dict is encoded to JSON text."""
        assert orjson.loads(str(_json_text({"a": 1}))) == {"a": 1}

    def test_encodes_list(self) -> None:
        """Test that a non-empty list is encoded to JSON text."""
        assert orjson.loads(str(_json_text([1, 2]))) == [1, 2]
