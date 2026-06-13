"""Tests for the keg linker.

Almost all tests are UNIT tests: they build a synthetic keg under a throwaway
prefix and assert on the resulting symlinks, so they run anywhere (no macOS, no
brew). They cover the per-directory strategy (link-whole vs mkpath-and-descend
vs skip), relative symlink targets, the linked record, conflict detection,
overwrite, keg-only suppression, and etc preservation.

The single INTEGRATION test (marked, macOS + brew only) is the fidelity check:
it diffs our *planned* link set for a real installed keg against the symlinks
brew actually created in the prefix — non-destructively, via the internal
_build_plan, without mutating the real prefix.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import brewery.providers.linker as linker
from brewery.providers.linker import LinkError, link_keg


def build_keg(prefix: Path, name: str = "openssl@3", version: str = "3.0") -> Path:
    """Create a synthetic Cellar keg with a representative layout."""
    keg = prefix / "Cellar" / name / version
    for d in [
        "bin",
        "lib/pkgconfig",
        "lib/engines-3",
        "include/openssl",
        "share/man/man1",
        "share/doc/openssl",
        ".brew",
    ]:
        (keg / d).mkdir(parents=True)
    (keg / "bin" / "openssl").write_text("#!/bin/sh\n")
    (keg / "lib" / "libssl.3.dylib").write_text("dylib")
    (keg / "lib" / "pkgconfig" / "openssl.pc").write_text("pc")
    (keg / "lib" / "engines-3" / "capi.dylib").write_text("engine")
    (keg / "include" / "openssl" / "ssl.h").write_text("h")
    (keg / "share" / "man" / "man1" / "openssl.1").write_text("man")
    (keg / "share" / "doc" / "openssl" / "README").write_text("doc")
    (keg / "INSTALL_RECEIPT.json").write_text("{}")
    (keg / ".brew" / f"{name}.rb").write_text("class")

    return keg


@pytest.fixture
def keg_and_prefix(tmp_path):
    prefix = tmp_path / "prefix"
    keg = build_keg(prefix)

    return keg, prefix


def _readlink(p: Path) -> str | None:
    return os.readlink(p) if p.is_symlink() else None


def test_bin_skips_absolute_target_symlinks(tmp_path):
    prefix = tmp_path / "prefix"
    keg = prefix / "Cellar" / "node" / "26.0"
    (keg / "bin").mkdir(parents=True)
    (keg / "bin" / "node").write_text("real binary")  # Real file -> link
    os.symlink(
        "../lib/node_modules/corepack/dist/corepack.js", keg / "bin" / "corepack"
    )  # Relative in-keg -> link
    os.symlink(
        "/usr/local/lib/node_modules/npm/bin/npm-cli.js", keg / "bin" / "npm"
    )  # Absolute -> skip

    res = link_keg(keg, prefix=prefix, name="node")
    assert "bin/node" in res.linked
    assert "bin/corepack" in res.linked
    assert "bin/npm" not in res.linked
    assert not (prefix / "bin" / "npm").exists()


def test_bin_files_are_relative_symlinks(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")
    link = prefix / "bin" / "openssl"
    assert link.is_symlink()
    assert _readlink(link) == "../Cellar/openssl@3/3.0/bin/openssl"
    assert link.resolve() == (keg / "bin" / "openssl").resolve()


def test_lib_dylib_symlinked_whole(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")
    assert (
        _readlink(prefix / "lib" / "libssl.3.dylib")
        == "../Cellar/openssl@3/3.0/lib/libssl.3.dylib"
    )


def test_lib_pkgconfig_is_mkpath_with_files_linked_inside(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")
    pc_dir = prefix / "lib" / "pkgconfig"
    assert pc_dir.is_dir() and not pc_dir.is_symlink()  # Real shared dir
    assert (
        _readlink(pc_dir / "openssl.pc")
        == "../../Cellar/openssl@3/3.0/lib/pkgconfig/openssl.pc"
    )


def test_non_mkpath_lib_subdir_linked_whole(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")

    # engines-3 is not in the mkpath set -> linked whole, not descended.
    eng = prefix / "lib" / "engines-3"
    assert eng.is_symlink()
    assert _readlink(eng) == "../Cellar/openssl@3/3.0/lib/engines-3"


def test_include_dir_linked_whole(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")
    inc = prefix / "include" / "openssl"
    assert inc.is_symlink()  # Not descended
    assert _readlink(inc) == "../Cellar/openssl@3/3.0/include/openssl"


def test_share_man_is_mkpath(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")
    assert (prefix / "share" / "man").is_dir() and not (
        prefix / "share" / "man"
    ).is_symlink()

    # man tree is mkpath all the way down: manN is a real dir, pages link as files
    assert (prefix / "share" / "man" / "man1").is_dir()
    assert not (prefix / "share" / "man" / "man1").is_symlink()
    assert (prefix / "share" / "man" / "man1" / "openssl.1").is_symlink()


def test_receipt_and_dotbrew_not_linked(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")
    assert not (prefix / "INSTALL_RECEIPT.json").exists()
    assert not (prefix / ".brew").exists()


def test_linked_record_created_and_relative(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")
    rec = prefix / "var" / "homebrew" / "linked" / "openssl@3"
    assert rec.is_symlink()
    assert _readlink(rec) == "../../../Cellar/openssl@3/3.0"
    assert rec.resolve() == keg.resolve()


def test_link_result_contents(keg_and_prefix):
    keg, prefix = keg_and_prefix
    res = link_keg(keg, prefix=prefix, name="openssl@3")
    assert "bin/openssl" in res.linked
    assert "lib/pkgconfig/openssl.pc" in res.linked
    assert "lib/pkgconfig" in res.created_dirs
    assert "include/openssl" in res.linked  # Linked whole


def test_relink_is_idempotent(keg_and_prefix):
    keg, prefix = keg_and_prefix
    link_keg(keg, prefix=prefix, name="openssl@3")
    res2 = link_keg(keg, prefix=prefix, name="openssl@3")

    # Everything already points at this keg -> reported as already_linked, no error
    assert "bin/openssl" in res2.already_linked
    assert res2.linked == []  # Nothing new to create
    assert (prefix / "bin" / "openssl").is_symlink()


def test_conflict_with_real_file_aborts_without_mutating(keg_and_prefix):
    keg, prefix = keg_and_prefix
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "openssl").write_text("USER FILE")
    with pytest.raises(LinkError, match="openssl"):
        link_keg(keg, prefix=prefix, name="openssl@3")

    # Pre-pass aborted: the user's file is intact and nothing else was linked
    assert (prefix / "bin" / "openssl").read_text() == "USER FILE"
    assert not (prefix / "lib" / "libssl.3.dylib").exists()
    assert not (prefix / "var" / "homebrew" / "linked" / "openssl@3").exists()


def test_conflict_with_other_keg_symlink_aborts(keg_and_prefix):
    keg, prefix = keg_and_prefix
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "openssl").symlink_to("../Cellar/other/1.0/bin/openssl")
    with pytest.raises(LinkError):
        link_keg(keg, prefix=prefix, name="openssl@3")


def test_overwrite_replaces_conflicting_file(keg_and_prefix):
    keg, prefix = keg_and_prefix
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "openssl").write_text("USER FILE")
    res = link_keg(keg, prefix=prefix, name="openssl@3", overwrite=True)
    link = prefix / "bin" / "openssl"
    assert link.is_symlink() and link.resolve() == (keg / "bin" / "openssl").resolve()
    assert "bin/openssl" in res.linked


def test_keg_only_is_a_noop(keg_and_prefix):
    keg, prefix = keg_and_prefix
    res = link_keg(keg, prefix=prefix, name="openssl@3", keg_only=True)
    assert res.linked == [] and res.created_dirs == []
    assert not (prefix / "bin" / "openssl").exists()
    assert not (prefix / "var" / "homebrew" / "linked" / "openssl@3").exists()


def test_etc_existing_config_preserved(tmp_path):
    prefix = tmp_path / "prefix"
    keg = build_keg(prefix)
    (keg / "etc").mkdir()
    (keg / "etc" / "foo.conf").write_text("default")
    (prefix / "etc").mkdir()
    (prefix / "etc" / "foo.conf").write_text("USER EDITED")
    res = link_keg(keg, prefix=prefix, name="openssl@3")
    assert (prefix / "etc" / "foo.conf").read_text() == "USER EDITED"  # Preserved
    assert (
        "etc/foo.conf" in res.already_linked
    )  # Rreated as already-satisfied, not a conflict


def test_etc_new_file_is_linked(tmp_path):
    prefix = tmp_path / "prefix"
    keg = build_keg(prefix)
    (keg / "etc").mkdir()
    (keg / "etc" / "new.conf").write_text("default")
    link_keg(keg, prefix=prefix, name="openssl@3")
    assert (prefix / "etc" / "new.conf").is_symlink()


def test_missing_eligible_dir_is_skipped(tmp_path):
    prefix = tmp_path / "prefix"
    keg = prefix / "Cellar" / "tiny" / "1.0"
    (keg / "lib").mkdir(parents=True)
    (keg / "lib" / "libtiny.dylib").write_text("x")  # No bin/include/share
    res = link_keg(keg, prefix=prefix, name="tiny")
    assert res.linked == ["lib/libtiny.dylib"]


_CANDIDATES = ["gettext", "python@3.13", "python@3.14", "node", "openssl@3"]


@pytest.mark.integration
@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("brew") is None,
    reason="requires macOS with Homebrew",
)
def test_plan_matches_brew_links():
    """Non-destructive fidelity check: our planned links for a real keg must
    match the symlinks brew actually created. Diffs point straight at strategy
    gaps to extend."""
    prefix = Path(
        subprocess.run(
            ["brew", "--prefix"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )

    formula = keg = None
    for cand in _CANDIDATES:
        cellar = prefix / "Cellar" / cand
        if not cellar.is_dir():
            continue
        versions = [p for p in cellar.iterdir() if p.is_dir()]

        # Only useful if it's actually linked (not keg-only / unlinked)
        record = prefix / "var" / "homebrew" / "linked" / cand
        if versions and record.is_symlink():
            formula, keg = cand, versions[-1]
            break
    if keg is None:
        pytest.skip("none of the candidate formulae are installed and linked")

    plan = linker._build_plan(keg, prefix)
    # Against an already-linked keg, every target lands in `already`, not `links`;
    # the set we'd *ensure* is linked is the union of both.
    brewery_links = {str(dst) for dst, _ in plan.links} | {str(p) for p in plan.already}

    # brew's real links into this keg, restricted to the eligible roots.
    keg_real = os.path.realpath(keg)
    brew_links: set[str] = set()
    for sub in linker._ELIGIBLE:
        root = prefix / sub
        if root.is_dir() and not root.is_symlink():
            brew_links |= _symlinks_into(root, keg_real)

    missing = brew_links - brewery_links  # Sstrategy gap
    spurious = brewery_links - brew_links  # Over-linking
    assert not spurious, (
        f"{formula}: would create links, brew did not: {sorted(spurious)}"
    )
    assert not missing, f"{formula}: strategy gap, brew links missed: {sorted(missing)}"


def _symlinks_into(root: Path, keg_real: str) -> set[str]:
    """Symlinks under root (descending real dirs only) that resolve into keg_real."""
    found: set[str] = set()
    stack = [str(root)]
    while stack:
        with os.scandir(stack.pop()) as it:
            for e in it:
                if e.is_symlink():
                    if os.path.realpath(e.path).startswith(keg_real):
                        found.add(e.path)

                elif e.is_dir(follow_symlinks=False):
                    stack.append(e.path)

    return found
