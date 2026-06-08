"""Integration tests for the Cellar/Caskroom filesystem scanner."""

from __future__ import annotations

import time
from pathlib import Path

import orjson
import pytest

from brewery.core.config import BreweryENV
from brewery.core.fs_state import (
    is_effectively_linked,
    linked_names,
    pinned_names,
    scan_installed,
)
from brewery.core.models import PackageKind

pytestmark = pytest.mark.integration


class Brew:
    """Builds a hermetic Homebrew prefix on disk for the scanner to read."""

    def __init__(self, root: Path) -> None:
        self.prefix = root / "homebrew"
        self.cellar = self.prefix / "Cellar"
        self.caskroom = self.prefix / "Caskroom"
        self.cellar.mkdir(parents=True)
        self.caskroom.mkdir(parents=True)

    @property
    def env(self) -> BreweryENV:
        return BreweryENV(
            prefix=self.prefix, cellar=self.cellar, caskroom=self.caskroom
        )

    def formula(
        self,
        name: str,
        version: str,
        *,
        receipt: dict | None = None,
        link_opt: bool = True,
        stale: list[str] | None = None,
    ) -> Path:
        """Create a Cellar keg, optional receipt, optional stale versions, opt link."""
        keg = self.cellar / name / version
        keg.mkdir(parents=True)
        for sv in stale or []:
            (self.cellar / name / sv).mkdir(parents=True)
        if receipt is not None:
            (keg / "INSTALL_RECEIPT.json").write_bytes(orjson.dumps(receipt))
        if link_opt:
            opt_dir = self.prefix / "opt"
            opt_dir.mkdir(exist_ok=True)
            (opt_dir / name).symlink_to(keg)
        return keg

    def cask(self, token: str, versions: list[str]) -> Path:
        """Create a Caskroom token directory with one or more version subdirs."""
        token_dir = self.caskroom / token
        token_dir.mkdir(parents=True)
        for v in versions:
            (token_dir / v).mkdir()
            # Stagger mtimes so 'most recent' is deterministic
            time.sleep(0.01)
        return token_dir

    def link(self, name: str) -> None:
        """Mark a formula linked in brew's bookkeeping directory."""
        d = self.prefix / "var" / "homebrew" / "linked"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).touch()

    def pin(self, name: str) -> None:
        """Mark a formula pinned in brew's bookkeeping directory."""
        d = self.prefix / "var" / "homebrew" / "pinned"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).touch()


@pytest.fixture
def brew(tmp_path) -> Brew:
    """A fresh hermetic Homebrew layout."""
    return Brew(tmp_path)


def _by_name(records) -> dict:
    return {r.name: r for r in records}


def full_receipt(**overrides) -> dict:
    """A representative INSTALL_RECEIPT.json payload."""
    receipt = {
        "installed_on_request": True,
        "installed_as_dependency": False,
        "time": 1_700_000_000,
        "source": {
            "spec": "stable",
            "tap": "homebrew/core",
            "versions": {"version_scheme": 2},
        },
        "runtime_dependencies": [
            {"full_name": "openssl@3"},
            {"full_name": "ca-certificates"},
        ],
    }
    receipt.update(overrides)
    return receipt


class TestScanFormulae:
    """Tests for scanning installed formulae."""

    def test_basic_formula_from_receipt(self, brew):
        """Test that a formula's record is built from its install receipt."""
        brew.formula("wget", "1.21.4", receipt=full_receipt())
        brew.link("wget")
        rec = _by_name(scan_installed(brew.env))["wget"]
        assert rec.kind == PackageKind.FORMULA
        assert rec.version == "1.21.4"
        assert rec.tap == "homebrew/core"
        assert rec.version_scheme == 2
        assert rec.installed_on_request is True
        assert rec.installed_as_dependency is False
        assert rec.deps == ["openssl@3", "ca-certificates"]

    def test_version_and_revision_split(self, brew):
        """Test that a keg name with a revision suffix is split correctly."""
        brew.formula("openssl", "3.2.1_1", receipt=full_receipt())
        rec = _by_name(scan_installed(brew.env))["openssl"]
        assert rec.version == "3.2.1"
        assert rec.revision == 1

    def test_head_spec_detected(self, brew):
        """Test that a HEAD install is flagged from the receipt spec."""
        brew.formula(
            "neovim", "HEAD-abc123", receipt=full_receipt(source={"spec": "head"})
        )
        rec = _by_name(scan_installed(brew.env))["neovim"]
        assert rec.head is True

    def test_install_time_from_receipt_epoch(self, brew):
        """Test that the install time comes from the receipt epoch when present."""
        brew.formula("wget", "1.0", receipt=full_receipt(time=1_700_000_000))
        rec = _by_name(scan_installed(brew.env))["wget"]
        assert rec.installed_on is not None
        assert int(rec.installed_on.timestamp()) == 1_700_000_000

    def test_missing_receipt_falls_back_to_mtime(self, brew):
        """Test that a keg with no receipt still yields a record via mtime.

        Flags that depend on the receipt are left at their defaults.
        """
        brew.formula("manual", "1.0", receipt=None)
        rec = _by_name(scan_installed(brew.env))["manual"]
        assert rec.version == "1.0"
        assert rec.installed_on is not None
        assert rec.deps == []
        assert rec.installed_on_request is False

    def test_corrupt_receipt_falls_back(self, brew):
        """Test that an unparseable receipt is treated as absent."""
        keg = brew.formula("broken", "1.0", receipt=None)
        (keg / "INSTALL_RECEIPT.json").write_text("{not json")
        rec = _by_name(scan_installed(brew.env))["broken"]
        assert rec.version == "1.0"
        assert rec.deps == []

    def test_stale_versions_recorded(self, brew):
        """Test that non-active version dirs are listed as stale."""
        brew.formula("wget", "1.21.4", receipt=full_receipt(), stale=["1.21.3"])
        rec = _by_name(scan_installed(brew.env))["wget"]
        assert rec.stale_versions == ["1.21.3"]

    def test_formula_with_no_keg_is_skipped(self, brew):
        """Test that an empty formula directory produces no record."""
        (brew.cellar / "ghost").mkdir()
        assert "ghost" not in _by_name(scan_installed(brew.env))

    def test_empty_cellar_yields_no_formulae(self, brew):
        """Test that an empty Cellar produces no formula records."""
        recs = [r for r in scan_installed(brew.env) if r.kind == PackageKind.FORMULA]
        assert recs == []


class TestScanCasks:
    """Tests for scanning installed casks."""

    def test_basic_cask(self, brew):
        """Test that a cask record is built from its Caskroom directory."""
        brew.cask("firefox", ["120.0"])
        rec = _by_name(scan_installed(brew.env))["firefox"]
        assert rec.kind == PackageKind.CASK
        assert rec.version == "120.0"
        assert rec.installed_on is not None

    def test_most_recent_version_is_active(self, brew):
        """Test that the newest version dir is chosen as active, rest are stale."""
        brew.cask("iina", ["1.3.0", "1.4.1"])  # 1.4.1 created later
        rec = _by_name(scan_installed(brew.env))["iina"]
        assert rec.version == "1.4.1"
        assert rec.stale_versions == ["1.3.0"]

    def test_cask_with_no_version_dir_skipped(self, brew):
        """Test that an empty token directory produces no record."""
        (brew.caskroom / "empty").mkdir()
        assert "empty" not in _by_name(scan_installed(brew.env))

    def test_metadata_dir_ignored(self, brew):
        """Test that a hidden .metadata dir is not treated as a version.

        _children excludes dotted entries, so a token whose only other child is
        a real version resolves to that version.
        """
        token_dir = brew.cask("slack", ["4.36.0"])
        (token_dir / ".metadata").mkdir()
        rec = _by_name(scan_installed(brew.env))["slack"]
        assert rec.version == "4.36.0"
        assert rec.stale_versions == []


class TestLinkPinState:
    """Tests for linked and pinned flag derivation."""

    def test_linked_from_bookkeeping(self, brew):
        """Test that a formula in the linked dir is flagged linked."""
        brew.formula("wget", "1.0", receipt=full_receipt())
        brew.link("wget")
        rec = _by_name(scan_installed(brew.env))["wget"]
        assert rec.linked is True

    def test_not_linked_when_absent_from_bookkeeping(self, brew):
        """Test that a formula absent from a present linked dir is not linked.

        Once the linked bookkeeping dir exists, membership is authoritative;
        a formula not listed there is unlinked even if its opt link exists.
        """
        brew.formula("wget", "1.0", receipt=full_receipt())
        brew.formula("keg-only-lib", "1.0", receipt=full_receipt())
        brew.link("wget")  # Creates the linked dir, lists only wget
        recs = _by_name(scan_installed(brew.env))
        assert recs["wget"].linked is True
        assert recs["keg-only-lib"].linked is False

    def test_pinned_flag(self, brew):
        """Test that a pinned formula is flagged pinned."""
        brew.formula("wget", "1.0", receipt=full_receipt())
        brew.pin("wget")
        rec = _by_name(scan_installed(brew.env))["wget"]
        assert rec.pinned is True

    def test_not_pinned_by_default(self, brew):
        """Test that a formula not in the pinned dir is not pinned."""
        brew.formula("wget", "1.0", receipt=full_receipt())
        rec = _by_name(scan_installed(brew.env))["wget"]
        assert rec.pinned is False

    def test_fallback_uses_opt_link_when_no_bookkeeping(self, brew):
        """Test that with no linked dir, the opt-link heuristic decides linkage.

        The fallback resolves opt/<name> and looks for a bin/sbin symlink into
        the keg; with one present the formula is effectively linked.
        """
        keg = brew.formula("wget", "1.0", receipt=full_receipt())
        bin_dir = brew.prefix / "bin"
        bin_dir.mkdir()
        (keg / "bin").mkdir()
        exe = keg / "bin" / "wget"
        exe.touch()
        (bin_dir / "wget").symlink_to(exe)
        # No linked bookkeeping dir created -> fallback path
        rec = _by_name(scan_installed(brew.env))["wget"]
        assert rec.linked is True


class TestBookkeepingHelpers:
    """Tests for the linked/pinned name readers in isolation."""

    def test_linked_names_none_when_absent(self, brew):
        """Test that a missing linked dir returns None (signals fallback)."""
        assert linked_names(brew.prefix) is None

    def test_pinned_names_empty_when_absent(self, brew):
        """Test that a missing pinned dir returns an empty set, not None."""
        assert pinned_names(brew.prefix) == set()

    def test_linked_names_reads_entries(self, brew):
        """Test that linked names are read from the bookkeeping dir."""
        brew.link("wget")
        brew.link("curl")
        assert linked_names(brew.prefix) == {"wget", "curl"}

    def test_bookkeeping_ignores_hidden(self, brew):
        """Test that hidden entries in the bookkeeping dir are skipped."""
        brew.link("wget")
        (brew.prefix / "var" / "homebrew" / "linked" / ".DS_Store").touch()
        assert linked_names(brew.prefix) == {"wget"}


class TestIsEffectivelyLinked:
    """Tests for the path-independent link heuristic."""

    def test_missing_opt_link_is_not_linked(self, brew):
        """Test that an absent opt link means not effectively linked."""
        brew.formula("wget", "1.0", receipt=full_receipt(), link_opt=False)
        assert is_effectively_linked("wget", brew.env) is False

    def test_opt_link_without_bin_symlink_is_not_linked(self, brew):
        """Test that an opt link with no bin/sbin symlink is not enough."""
        brew.formula("wget", "1.0", receipt=full_receipt())  # opt link only
        assert is_effectively_linked("wget", brew.env) is False

    def test_opt_link_with_bin_symlink_is_linked(self, brew):
        """Test that an opt link plus a bin symlink into the keg counts as linked."""
        keg = brew.formula("wget", "1.0", receipt=full_receipt())
        bin_dir = brew.prefix / "bin"
        bin_dir.mkdir()
        (keg / "bin").mkdir()
        exe = keg / "bin" / "wget"
        exe.touch()
        (bin_dir / "wget").symlink_to(exe)
        assert is_effectively_linked("wget", brew.env) is True


class TestReverseDeps:
    """Tests for used_by derivation across installed records."""

    def test_used_by_populated(self, brew):
        """Test that a dependency lists its installed dependents."""
        brew.formula(
            "curl",
            "1.0",
            receipt=full_receipt(runtime_dependencies=[{"full_name": "openssl@3"}]),
        )
        brew.formula(
            "git",
            "1.0",
            receipt=full_receipt(runtime_dependencies=[{"full_name": "openssl@3"}]),
        )
        brew.formula(
            "openssl@3",
            "3.2.1",
            receipt=full_receipt(runtime_dependencies=[]),
        )
        rec = _by_name(scan_installed(brew.env))["openssl@3"]
        assert rec.used_by == ["curl", "git"]

    def test_tapped_dep_matched_by_leaf_name(self, brew):
        """Test that a tap-qualified dep matches the installed leaf package.

        A dependency given as 'homebrew/core/openssl@3' should still attribute
        to the installed 'openssl@3' via its leaf name.
        """
        brew.formula(
            "curl",
            "1.0",
            receipt=full_receipt(
                runtime_dependencies=[{"full_name": "homebrew/core/openssl@3"}]
            ),
        )
        brew.formula(
            "openssl@3", "3.2.1", receipt=full_receipt(runtime_dependencies=[])
        )
        rec = _by_name(scan_installed(brew.env))["openssl@3"]
        assert rec.used_by == ["curl"]

    def test_no_dependents_is_empty(self, brew):
        """Test that a leaf package with no dependents has empty used_by."""
        brew.formula("wget", "1.0", receipt=full_receipt(runtime_dependencies=[]))
        rec = _by_name(scan_installed(brew.env))["wget"]
        assert rec.used_by == []
