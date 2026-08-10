"""Unit tests for the keg substitution map and formula tokens."""

from __future__ import annotations

import pytest
from relocator_helpers import (
    _dep,
)

from brewery.core import host
from brewery.core.host import Platform
from brewery.providers.relocator import substitutions as subs_mod

pytestmark = pytest.mark.unit


class TestSubstitution:
    """Tests for the substitution mapping."""

    def test_substitutions_longest_token_first(self, subs) -> None:
        """Tests that the longest token is matched first."""
        # @@HOMEBREW_PREFIX@@ must be matched before @@HOMEBREW_CELLAR@@.
        keys = list(subs.keys())
        assert keys == sorted(keys, key=len, reverse=True)

    def test_apply_replaces_cellar_even_when_longer_than_token(self, subs) -> None:
        """Tests that the substitution is applied even when the replacement is longer."""
        # Cellar expands to a path LONGER than its placeholder
        out = subs_mod._apply(b"@@HOMEBREW_CELLAR@@/foo/1.0/lib", subs)
        assert out == b"/opt/homebrew/Cellar/foo/1.0/lib"

    def test_apply_noop_without_marker(self, subs) -> None:
        """Tests that the substitution is a no-op when the marker is not present."""
        assert (
            subs_mod._apply(b"/usr/lib/libSystem.dylib", subs)
            == b"/usr/lib/libSystem.dylib"
        )


class TestFormulaTokens:
    """Tests for the per-formula token map (brew's prepare_relocation_to_locations)."""

    def test_perl_from_system_when_uses_from_macos(
        self, brew_paths, system_perl
    ) -> None:
        """Test when perl is uses_from_macos, so it is not a dep on macOS.

        The shebang must point at the system perl the bottle was built against,
        not at an opt/perl that was never installed.
        """
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"],
            name="cloc",
            runtime_deps=[],
            built_on={"preferred_perl": "5.34"},
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/usr/bin/perl5.34"

    def test_perl_from_brewed_perl_when_declared(self, brew_paths, system_perl) -> None:
        """Tests that a formula declaring perl gets the opt-linked brewed perl."""
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"],
            name="ack",
            runtime_deps=[_dep("perl")],
            built_on={"preferred_perl": "5.34"},
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/opt/homebrew/opt/perl/bin/perl"

    def test_perl_itself_gets_brewed_perl(self, brew_paths, system_perl) -> None:
        """Tests that the perl formula resolves to its own opt path."""
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"], name="perl", runtime_deps=[]
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/opt/homebrew/opt/perl/bin/perl"

    def test_indirect_perl_dep_uses_system_perl(self, brew_paths, system_perl) -> None:
        """Test that a perl pulled in transitively is not a declared dep, so brew uses system perl."""
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"],
            name="cloc",
            runtime_deps=[_dep("perl", declared_directly=False)],
            built_on={"preferred_perl": "5.34"},
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/usr/bin/perl5.34"

    @pytest.mark.parametrize(
        "built_on",
        [None, {}, {"preferred_perl": "5.99"}, {"preferred_perl": "not-a-version"}],
        ids=["no-tab", "empty", "absent-from-host", "malformed"],
    )
    def test_perl_falls_back_to_host_preferred(
        self, brew_paths, system_perl, monkeypatch, built_on
    ) -> None:
        """Tests that an unusable tab value falls back to this host's preferred perl."""
        monkeypatch.setattr(
            host,
            "current_platform",
            lambda: Platform(arch="arm64", os="macos", macos_major=12),
        )
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"], name="cloc", runtime_deps=[], built_on=built_on
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/usr/bin/perl5.30"

    def test_java_omitted_without_openjdk_dep(self, brew_paths, system_perl) -> None:
        """Tests that java is left undefined when no openjdk dep exists."""
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"], name="cloc", runtime_deps=[_dep("zlib")]
        )
        assert "@@HOMEBREW_JAVA@@" not in tokens

    @pytest.mark.parametrize("dep", ["openjdk", "openjdk@21", "openjdk@11.0"])
    def test_java_resolved_from_openjdk_dep(self, brew_paths, system_perl, dep) -> None:
        """Tests that java resolves to JAVA_HOME inside the openjdk dep's bundle.

        On macOS that is libexec/openjdk.jdk/Contents/Home, not libexec itself.
        """
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"], name="jenkins", runtime_deps=[_dep(dep)]
        )
        assert tokens["@@HOMEBREW_JAVA@@"] == (
            f"/opt/homebrew/opt/{dep}/libexec/openjdk.jdk/Contents/Home"
        )

    def test_openjdk_lookalike_dep_ignored(self, brew_paths, system_perl) -> None:
        """Tests that a dep merely containing 'openjdk' does not resolve java."""
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"],
            name="jenkins",
            runtime_deps=[_dep("openjdk-headless")],
        )
        assert "@@HOMEBREW_JAVA@@" not in tokens

    def test_perl_unversioned_on_linux(self, brew_paths, monkeypatch) -> None:
        """Tests that Linux uses the unversioned /usr/bin/perl (no system perlX.Y)."""
        monkeypatch.setattr(
            subs_mod, "current_platform", lambda: Platform(arch="amd64", os="linux")
        )
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"],
            name="cloc",
            runtime_deps=[],
            built_on={"preferred_perl": "5.34"},
        )
        assert tokens["@@HOMEBREW_PERL@@"] == "/usr/bin/perl"

    def test_java_resolves_to_libexec_on_linux(self, brew_paths, monkeypatch) -> None:
        """Tests that on Linux JAVA_HOME is the keg's libexec, not the .jdk bundle."""
        monkeypatch.setattr(
            subs_mod, "current_platform", lambda: Platform(arch="amd64", os="linux")
        )
        tokens = subs_mod.formula_tokens(
            brew_paths["prefix"], name="jenkins", runtime_deps=[_dep("openjdk@21")]
        )
        assert tokens["@@HOMEBREW_JAVA@@"] == "/opt/homebrew/opt/openjdk@21/libexec"
