"""Tests for the INSTALL_RECEIPT.json builder."""

from __future__ import annotations

import unittest.mock as mock

import orjson
import pytest

import brewery.providers.receipt as r
from brewery.core.config import get_brewery_env
from brewery.providers.receipt import RuntimeDependency, Source, build_receipt, dumps

SQLITE = """\
{
  "homebrew_version": "5.1.15-12-g2d9bb4c",
  "used_options": [],
  "unused_options": [],
  "built_as_bottle": true,
  "poured_from_bottle": true,
  "loaded_from_api": true,
  "loaded_from_internal_api": false,
  "installed_on_request": false,
  "changed_files": [
    "lib/pkgconfig/sqlite3.pc"
  ],
  "time": 1780682171,
  "source_modified_time": 1780514862,
  "compiler": "clang",
  "aliases": [
    "sqlite3"
  ],
  "runtime_dependencies": [
    {
      "full_name": "readline",
      "version": "8.3.3",
      "revision": 0,
      "bottle_rebuild": 0,
      "pkg_version": "8.3.3",
      "declared_directly": true
    }
  ],
  "source": {
    "spec": "stable",
    "versions": {
      "stable": "3.53.2",
      "head": null,
      "version_scheme": 0,
      "compatibility_version": null
    },
    "path": "/Users/rdawebb/Library/Caches/Homebrew/api/formula.jws.json",
    "tap_git_head": null,
    "tap": "homebrew/core"
  },
  "arch": "x86_64",
  "built_on": {
    "os": "Macintosh",
    "os_version": "macOS 15.7",
    "cpu_family": "penryn",
    "xcode": "26.3",
    "clt": "26.3.0.0.1.1771626560",
    "preferred_perl": "5.34"
  }
}"""

OPENSSL = """\
{
  "homebrew_version": "5.1.5-3-g9f7d5c5",
  "used_options": [],
  "unused_options": [],
  "built_as_bottle": true,
  "poured_from_bottle": true,
  "loaded_from_api": true,
  "loaded_from_internal_api": false,
  "installed_on_request": true,
  "changed_files": [
    "bin/c_rehash",
    "lib/pkgconfig/libcrypto.pc",
    "lib/pkgconfig/libssl.pc",
    "lib/pkgconfig/openssl.pc"
  ],
  "time": 1777554106,
  "source_modified_time": 1775564277,
  "compiler": "clang",
  "aliases": [
    "openssl",
    "openssl@3.6"
  ],
  "runtime_dependencies": [
    {
      "full_name": "ca-certificates",
      "version": "2026-03-19",
      "revision": 0,
      "bottle_rebuild": 0,
      "pkg_version": "2026-03-19",
      "declared_directly": true
    }
  ],
  "source": {
    "spec": "stable",
    "versions": {
      "stable": "3.6.2",
      "head": null,
      "version_scheme": 0,
      "compatibility_version": null
    },
    "path": "/Users/rdawebb/Library/Caches/Homebrew/api/formula.jws.json",
    "tap_git_head": null,
    "tap": "homebrew/core"
  },
  "arch": "x86_64",
  "built_on": {
    "os": "Macintosh",
    "os_version": "macOS 15.7",
    "cpu_family": "penryn",
    "xcode": "26.3",
    "clt": "26.3.0.0.1.1771626560",
    "preferred_perl": "5.34"
  }
}"""

CA_CERTIFICATES = """\
{
  "homebrew_version": "5.1.11-89-g34257b4",
  "used_options": [],
  "unused_options": [],
  "built_as_bottle": true,
  "poured_from_bottle": true,
  "loaded_from_api": true,
  "loaded_from_internal_api": false,
  "installed_on_request": false,
  "changed_files": [],
  "time": 1778756708,
  "source_modified_time": 1778728322,
  "compiler": "gcc-12",
  "aliases": [],
  "runtime_dependencies": [],
  "source": {
    "spec": "stable",
    "versions": {
      "stable": "2026-05-14",
      "head": null,
      "version_scheme": 0,
      "compatibility_version": null
    },
    "path": "/Users/rdawebb/Library/Caches/Homebrew/api/formula.jws.json",
    "tap_git_head": null,
    "tap": "homebrew/core"
  },
  "arch": "x86_64",
  "built_on": null
}"""

# Tab runtime_dependencies carry an extra compatibility_version the receipt drops
SQLITE_TAB_DEPS = [
    {
        "full_name": "readline",
        "version": "8.3.3",
        "revision": 0,
        "bottle_rebuild": 0,
        "pkg_version": "8.3.3",
        "declared_directly": True,
        "compatibility_version": 1,
    }
]
OPENSSL_TAB_DEPS = [
    {
        "full_name": "ca-certificates",
        "version": "2026-03-19",
        "revision": 0,
        "bottle_rebuild": 0,
        "pkg_version": "2026-03-19",
        "declared_directly": True,
        "compatibility_version": 1,
    }
]


def _rebuild(o: dict, tab_deps: list[dict]) -> dict:
    """Rebuild from a parsed receipt, deps supplied in tab shape (with
    compatibility_version) to exercise from_tab stripping.

    Args:
        o: The parsed receipt.
        tab_deps: The tab-shaped dependencies.

    Returns:
        The rebuilt receipt.
    """
    src, ver = o["source"], o["source"]["versions"]

    return build_receipt(
        homebrew_version=o["homebrew_version"],
        changed_files=o["changed_files"],
        source_modified_time=o["source_modified_time"],
        compiler=o["compiler"],
        runtime_dependencies=[RuntimeDependency.from_tab(d) for d in tab_deps],
        built_on=o["built_on"],
        installed_on_request=o["installed_on_request"],
        time=o["time"],
        source=Source(
            stable_version=ver["stable"],
            api_path=src["path"],
            version_scheme=ver["version_scheme"],
            tap=src["tap"],
        ),
        aliases=o["aliases"],
    )


@pytest.mark.parametrize(
    "original,tab_deps",
    [
        (SQLITE, SQLITE_TAB_DEPS),
        (OPENSSL, OPENSSL_TAB_DEPS),
        (CA_CERTIFICATES, []),
    ],
    ids=["sqlite", "openssl", "ca-certificates"],
)
def test_round_trip_is_byte_exact(original, tab_deps) -> None:
    """Test that round-tripping a receipt preserves byte-for-byte equality."""
    with mock.patch.object(r.platform, "machine", lambda: "x86_64"):
        assert dumps(_rebuild(orjson.loads(original), tab_deps)) == original


def test_all_bottle_fills_arch_from_host_and_nulls_built_on() -> None:
    """Test that an all-bottle tab fills arch from host and nulls built_on."""
    # Simulate an all-bottle tab: arch=None, built_on=None
    env = get_brewery_env()
    parsed = orjson.loads(CA_CERTIFICATES)
    with mock.patch.object(r.platform, "machine", lambda: "x86_64"):
        built = build_receipt(
            homebrew_version=parsed["homebrew_version"],
            changed_files=[],
            source_modified_time=parsed["source_modified_time"],
            compiler="gcc-12",
            runtime_dependencies=[],
            built_on=None,
            installed_on_request=False,
            time=parsed["time"],
            source=Source(stable_version="2026-05-14", api_path=str(env.api_path)),
            aliases=[],
        )
    assert built["arch"] == "x86_64"  # Filled from host
    assert built["built_on"] is None  # Written as null
    assert dumps(built) == CA_CERTIFICATES


def test_from_tab_drops_compatibility_version() -> None:
    """Test that from_tab drops compatibility_version."""
    d = RuntimeDependency.from_tab(OPENSSL_TAB_DEPS[0]).to_dict()
    assert "compatibility_version" not in d
    assert list(d) == [
        "full_name",
        "version",
        "revision",
        "bottle_rebuild",
        "pkg_version",
        "declared_directly",
    ]
    assert d["full_name"] == "ca-certificates" and d["declared_directly"] is True


def test_pkg_version_defaults_to_version() -> None:
    """Test that pkg_version defaults to version."""
    assert RuntimeDependency("readline", "8.3.3").to_dict()["pkg_version"] == "8.3.3"


def test_top_level_field_order() -> None:
    """Test that the top-level fields are in the expected order."""
    receipt = _rebuild(orjson.loads(SQLITE), SQLITE_TAB_DEPS)
    assert list(receipt) == [
        "homebrew_version",
        "used_options",
        "unused_options",
        "built_as_bottle",
        "poured_from_bottle",
        "loaded_from_api",
        "loaded_from_internal_api",
        "installed_on_request",
        "changed_files",
        "time",
        "source_modified_time",
        "compiler",
        "aliases",
        "runtime_dependencies",
        "source",
        "arch",
        "built_on",
    ]


def test_compiler_is_tab_sourced_not_constant() -> None:
    """Test that the compiler is sourced from the tab, not a constant."""
    receipt = _rebuild(orjson.loads(CA_CERTIFICATES), [])
    assert receipt["compiler"] == "gcc-12"


def test_changed_files_sorted_in_output() -> None:
    """Test that changed_files are sorted in the output."""
    receipt = build_receipt(
        homebrew_version="x",
        changed_files=["lib/z.pc", "bin/a", "lib/a.pc"],
        source_modified_time=1,
        compiler="clang",
        runtime_dependencies=[],
        built_on=None,
        installed_on_request=True,
        time=1,
        source=Source(stable_version="1.0", api_path="/p"),
        aliases=[],
    )
    assert receipt["changed_files"] == ["bin/a", "lib/a.pc", "lib/z.pc"]


def test_dumps_no_trailing_newline_and_null_built_on() -> None:
    """Test that dumps does not add a trailing newline and sets built_on to null."""
    text = dumps(_rebuild(orjson.loads(CA_CERTIFICATES), []))
    assert not text.endswith("\n")
    assert text.endswith('"built_on": null\n}')


def test_write_receipt_atomic_mode_and_content(tmp_path) -> None:
    """Test that write_receipt uses atomic mode and writes the correct content."""
    keg = tmp_path / "keg"
    keg.mkdir()
    receipt = _rebuild(orjson.loads(SQLITE), SQLITE_TAB_DEPS)
    path = r.write_receipt(keg, receipt)
    assert path == keg / "INSTALL_RECEIPT.json"
    assert path.read_text() == SQLITE
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert list(keg.glob("*.tmp")) == []


def test_current_arch_maps_machine(monkeypatch) -> None:
    """Test that current_arch maps to the machine architecture."""
    monkeypatch.setattr(r.platform, "machine", lambda: "arm64")
    assert r.current_arch() == "arm64"
