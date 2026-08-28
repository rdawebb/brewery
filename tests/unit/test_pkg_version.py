"""Unit tests for the Homebrew-compatible version comparator."""

from __future__ import annotations

import itertools

import pytest

from brewery.core.version import PkgVersion, compare_versions, is_head


class TestCompareVersions:
    """Tests for compare_versions."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("1.0.1", "1.0.0", id="patch_bump"),
            pytest.param("10.0", "9.0", id="numeric_not_lexical"),
            pytest.param("1.10", "1.9", id="numeric_minor_not_lexical"),
            pytest.param("1.0.26", "0.6.1", id="d12_local_keg_ahead_of_catalog"),
            pytest.param("1.2.3.1", "1.2.3", id="extra_numeric_component"),
            pytest.param("2024-01-05", "2023-12-31", id="date_style"),
            pytest.param("r123", "r99", id="prefixed_numeric"),
            pytest.param("1.2.3", "1.2.3rc1", id="release_beats_rc"),
            pytest.param("1.2.3", "1.2.3beta1", id="release_beats_beta"),
            pytest.param("1.2.3", "1.2.3alpha1", id="release_beats_alpha"),
            pytest.param("1.2.3p1", "1.2.3", id="patch_marker_beats_release"),
            pytest.param("1.2.3rc2", "1.2.3rc1", id="rc_suffix_ordering"),
            pytest.param("1.2.3rc1", "1.2.3pre1", id="rc_beats_pre"),
            pytest.param("1.2.3pre1", "1.2.3beta1", id="pre_beats_beta"),
            pytest.param("1.2.3beta1", "1.2.3alpha1", id="beta_beats_alpha"),
            pytest.param("1.2.3b1", "1.2.3a1", id="short_beta_beats_short_alpha"),
            pytest.param("1.2a", "1.2", id="trailing_string_beats_bare"),
        ],
    )
    def test_left_is_newer(self, left, right) -> None:
        """Test that the left version sorts after the right one, and vice versa."""
        assert compare_versions(left, right) == 1
        assert compare_versions(right, left) == -1

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("1.2.3", "1.2.3", id="identical"),
            pytest.param("1.0.0", "1.0", id="trailing_zero_padding"),
            pytest.param("1.0.0.0", "1", id="many_trailing_zeros"),
            pytest.param("1.2.3rc", "1.2.3rc0", id="absent_suffix_is_zero"),
        ],
    )
    def test_equal(self, left, right) -> None:
        """Test that versions differing only in padding compare equal."""
        assert compare_versions(left, right) == 0
        assert compare_versions(right, left) == 0

    def test_case_insensitive_tokens(self) -> None:
        """Test that marker tokens are recognised regardless of case."""
        assert compare_versions("1.2.3RC1", "1.2.3rc1") == 0
        assert compare_versions("1.2.3", "1.2.3RC1") == 1

    def test_ordering_is_total_over_a_chain(self) -> None:
        """Test that a full prerelease-to-post chain sorts in the documented order."""
        chain = [
            "1.2.3alpha1",
            "1.2.3beta1",
            "1.2.3pre1",
            "1.2.3rc1",
            "1.2.3",
            "1.2.3p1",
        ]

        for older, newer in itertools.pairwise(chain):
            assert compare_versions(newer, older) == 1, f"{newer} should beat {older}"


class TestHeadVersions:
    """Tests for HEAD ordering, which is settled before any tokenising."""

    @pytest.mark.parametrize(
        "version",
        ["HEAD", "HEAD-abc123", "HEAD-", "HEAD-a.b-c"],
        ids=["bare", "with_commit", "empty_commit", "punctuated_commit"],
    )
    def test_is_head(self, version) -> None:
        """Test that HEAD and HEAD-<commit> are recognised."""
        assert is_head(version) is True

    @pytest.mark.parametrize(
        "version",
        ["head", "HEADx", "1.2.3", "", "xHEAD"],
        ids=["lowercase", "suffixed", "release", "empty", "prefixed"],
    )
    def test_is_not_head(self, version) -> None:
        """Test that near-misses are not treated as HEAD."""
        assert is_head(version) is False

    @pytest.mark.parametrize(
        ("head", "release"),
        [
            pytest.param("HEAD", "1.2.3", id="bare_head"),
            pytest.param("HEAD-abc123", "1.2.3", id="head_with_commit"),
            pytest.param("HEAD-abc123", "99999", id="beats_large_numeric"),
            pytest.param("HEAD", "0", id="beats_zero"),
        ],
    )
    def test_head_beats_any_release(self, head, release) -> None:
        """Test that a HEAD build outranks every real version."""
        assert compare_versions(head, release) == 1
        assert compare_versions(release, head) == -1

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("HEAD", "HEAD-abc123", id="bare_vs_commit"),
            pytest.param("HEAD-abc123", "HEAD-def456", id="differing_commits"),
        ],
    )
    def test_heads_tie(self, left, right) -> None:
        """Test that the commit is not an ordering: any two HEADs are equal."""
        assert compare_versions(left, right) == 0
        assert compare_versions(right, left) == 0

    def test_head_keg_is_not_outdated_against_the_catalog(self) -> None:
        """Test the case the ordering exists for: a HEAD keg is never the older side."""
        assert PkgVersion("HEAD-abc123") > PkgVersion("1.2.3", 4)


class TestPkgVersionParse:
    """Tests for PkgVersion.parse."""

    @pytest.mark.parametrize(
        ("keg", "expected"),
        [
            pytest.param("1.2.3_4", ("1.2.3", 4), id="version_and_revision_split"),
            pytest.param("1.2.3", ("1.2.3", 0), id="no_underscore_zero_revision"),
            pytest.param("1.2.3_beta", ("1.2.3_beta", 0), id="non_digit_tail"),
            pytest.param("1_2_3", ("1_2", 3), id="last_underscore_group_wins"),
            pytest.param("1.2.3_", ("1.2.3_", 0), id="trailing_underscore_empty_tail"),
            pytest.param("1.2_3_4", ("1.2_3", 4), id="multiple_underscores"),
            pytest.param("", ("", 0), id="empty_name"),
        ],
    )
    def test_parse(self, keg, expected) -> None:
        """Test that a keg directory name splits into version and revision."""
        parsed = PkgVersion.parse(keg)

        assert (parsed.version, parsed.revision) == expected

    @pytest.mark.parametrize(
        "keg",
        ["1.2.3", "1.2.3_4", "1.2.3_beta", "1.21.4_2"],
        ids=["bare", "with_revision", "non_digit_tail", "real_keg"],
    )
    def test_parse_round_trips_through_str(self, keg) -> None:
        """Test that rendering a parsed keg name reproduces it."""
        assert str(PkgVersion.parse(keg)) == keg


class TestPkgVersionRendering:
    """Tests for PkgVersion.__str__, which is the display form across the CLI."""

    @pytest.mark.parametrize(
        ("version", "revision", "expected"),
        [
            pytest.param("1.2.3", 0, "1.2.3", id="zero_revision_bare_version"),
            pytest.param("1.2.3", None, "1.2.3", id="revision_defaults_to_zero"),
            pytest.param("1.2.3", 4, "1.2.3_4", id="positive_revision_appended"),
            pytest.param("1.2.3", -1, "1.2.3", id="negative_revision_ignored"),
            pytest.param("", 0, "", id="empty_version_no_revision"),
        ],
    )
    def test_str(self, version, revision, expected) -> None:
        """Test that the revision is appended with an underscore when positive."""
        # revision=None exercises the default-argument path
        pkg = (
            PkgVersion(version=version)
            if revision is None
            else PkgVersion(version=version, revision=revision)
        )

        assert str(pkg) == expected


class TestPkgVersionOrdering:
    """Tests for PkgVersion comparison, where the revision is a tiebreak."""

    def test_revision_breaks_a_tie(self) -> None:
        """Test that a higher revision on the same version sorts newer."""
        assert PkgVersion("1.2.3", 2) > PkgVersion("1.2.3", 1)
        assert PkgVersion("1.2.3", 0) < PkgVersion("1.2.3", 1)

    def test_version_outranks_revision(self) -> None:
        """Test that the upstream version decides before the revision is consulted."""
        assert PkgVersion("1.2.4", 0) > PkgVersion("1.2.3", 9)

    def test_revision_is_not_folded_into_the_version(self) -> None:
        """Test that `1.2.3` revision 4 is distinct from the version `1.2.3.4`."""
        assert PkgVersion("1.2.3", 4) < PkgVersion("1.2.3.4", 0)

    def test_equality_follows_ordering(self) -> None:
        """Test that padding-equivalent versions compare equal, not merely similar."""
        assert PkgVersion("1.0") == PkgVersion("1.0.0")

        # `_formula_outdated` reaches for `!=` on the scheme-bump branch
        assert (PkgVersion("1.0") != PkgVersion("1.0.0")) is False

    def test_comparison_against_other_types(self) -> None:
        """Test that comparing against a non-PkgVersion is not silently accepted."""
        assert PkgVersion("1.0") != "1.0"

        with pytest.raises(TypeError):
            _ = PkgVersion("1.0") < "1.0"  # ty: ignore[unsupported-operator]

    def test_sorting_a_list(self) -> None:
        """Test that total_ordering gives usable sort behaviour."""
        versions = [
            PkgVersion("1.2.3", 1),
            PkgVersion("1.2.3rc1"),
            PkgVersion("1.2.10"),
            PkgVersion("1.2.3"),
        ]

        assert [str(v) for v in sorted(versions)] == [
            "1.2.3rc1",
            "1.2.3",
            "1.2.3_1",
            "1.2.10",
        ]
