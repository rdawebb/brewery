"""Unit tests for the merge-on-read join of installed state and catalog."""

from __future__ import annotations

import pytest

from brewery.core.catalog import CaskRow, Catalog, FormulaRow
from brewery.core.fs_state import InstalledRecord
from brewery.core.merge import (
    catalog_info,
    merge,
    merge_one,
    search_packages,
)
from brewery.core.models import PackageKind, PackageStatus

pytestmark = pytest.mark.unit


def make_formula_row(
    name: str = "wget",
    *,
    desc: str | None = "retrieves files",
    tap: str | None = "homebrew/core",
    version: str = "1.21.4",
    revision: int = 0,
    version_scheme: int = 0,
    keg_only: bool = False,
    has_service: bool = False,
) -> FormulaRow:
    """Build a FormulaRow with sensible defaults for the fields merge reads.

    Args:
        name: The name of the formula.
        desc: A description of the formula.
        tap: The tap the formula belongs to.
        version: The version of the formula.
        revision: The revision of the formula.
        version_scheme: The version scheme of the formula.
        keg_only: Whether the formula is keg-only.
        has_service: Whether the formula has a service.

    Returns:
        A FormulaRow with the specified fields.
    """
    return FormulaRow(
        name=name,
        desc=desc,
        homepage=None,
        tap=tap,
        version=version,
        revision=revision,
        version_scheme=version_scheme,
        keg_only=keg_only,
        has_service=has_service,
        post_install=False,
        bottle_url=None,
        bottle_sha256=None,
        bottle_cellar=None,
        bottle_rebuild=0,
        deprecated=False,
        disabled=False,
    )


def make_cask_row(
    token: str = "firefox",
    *,
    name: str | None = "Firefox",
    desc: str | None = "web browser",
    tap: str | None = "homebrew/cask",
    version: str | None = "120.0",
) -> CaskRow:
    """Build a CaskRow with sensible defaults for the fields merge reads.

    Args:
        token: The token for the cask.
        name: The name of the cask.
        desc: A description of the cask.
        tap: The tap the cask belongs to.
        version: The version of the cask.

    Returns:
        A CaskRow with the specified fields.
    """
    return CaskRow(
        token=token,
        name=name,
        desc=desc,
        homepage=None,
        tap=tap,
        version=version,
        sha256=None,
        url=None,
        auto_updates=False,
        artifacts=None,
        depends_on=None,
        deprecated=False,
        disabled=False,
    )


def make_record(
    name: str = "wget",
    *,
    kind: PackageKind = PackageKind.FORMULA,
    version: str = "1.21.4",
    revision: int = 0,
    version_scheme: int | None = None,
    head: bool = False,
    linked: bool = True,
    pinned: bool = False,
    tap: str | None = None,
    deps: list[str] | None = None,
) -> InstalledRecord:
    """Build an InstalledRecord with defaults for the fields merge reads.

    Args:
        name: The name of the package.
        kind: The kind of the package (formula or cask).
        version: The version of the package.
        revision: The revision of the package.
        version_scheme: The version scheme of the package.
        head: Whether the package is a head version.
        linked: Whether the package is linked.
        pinned: Whether the package is pinned.
        tap: The tap the package belongs to.
        deps: The dependencies of the package.

    Returns:
        An InstalledRecord with the specified fields.
    """
    return InstalledRecord(
        name=name,
        kind=kind,
        version=version,
        revision=revision,
        version_scheme=version_scheme,
        head=head,
        linked=linked,
        pinned=pinned,
        tap=tap,
        deps=deps or [],
    )


class MockCatalog(Catalog):
    """Minimal stand-in exposing only the read methods merge.py calls."""

    def __init__(
        self,
        *,
        formulae: dict[str, FormulaRow] | None = None,
        casks: dict[str, CaskRow] | None = None,
        aliases: dict[str, str] | None = None,
        deps: dict[str, list[str]] | None = None,
        search_results: list[FormulaRow | CaskRow] | None = None,
    ) -> None:
        """Initialise a MockCatalog with the specified fields.

        Args:
            formulae: A mapping of formula names to their rows.
            casks: A mapping of cask tokens to their rows.
            aliases: A mapping of alias names to their real names.
            deps: A mapping of formula names to their dependencies.
            search_results: A list of search result rows.
        """
        self._formulae = formulae or {}
        self._casks = casks or {}
        self._aliases = aliases or {}
        self._deps = deps or {}
        self._search_results = search_results or []

    def get_formula(self, name: str) -> FormulaRow | None:
        """Get a formula by name.

        Args:
            name: The name of the formula.

        Returns:
            The formula row, or None if not found.
        """
        return self._formulae.get(name)

    def get_formulae(self, names: list[str]) -> dict[str, FormulaRow]:
        """Get multiple formulae by their names.

        Args:
            names: The names of the formulae.

        Returns:
            A mapping of formula names to their rows.
        """
        return {n: self._formulae[n] for n in names if n in self._formulae}

    def get_cask(self, token: str) -> CaskRow | None:
        """Get a cask by its token.

        Args:
            token: The token of the cask.

        Returns:
            The cask row, or None if not found.
        """
        return self._casks.get(token)

    def get_casks(self, tokens: list[str]) -> dict[str, CaskRow]:
        """Get multiple casks by their tokens.

        Args:
            tokens: The tokens of the casks.

        Returns:
            A mapping of cask tokens to their rows.
        """
        return {t: self._casks[t] for t in tokens if t in self._casks}

    def resolve_alias(self, name: str) -> str:
        """Resolve an alias to its real name.

        Args:
            name: The name of the alias.

        Returns:
            The real name of the formula or cask.
        """
        return self._aliases.get(name, name)

    def deps_of(self, name: str) -> list[str]:
        """Get the dependencies of a formula.

        Args:
            name: The name of the formula.

        Returns:
            A list of dependency names.
        """
        return self._deps.get(name, [])

    def search(self, query: str, limit: int = 50) -> list[FormulaRow | CaskRow]:
        """Search for formulae and casks matching a query.

        Args:
            query: The search query.
            limit: The maximum number of results to return.

        Returns:
            A list of matching formulae and casks.
        """
        return self._search_results


class TestMergeDispatch:
    """Tests for kind dispatch and ordering in merge / merge_one."""

    def test_merge_one_dispatches_formula(self) -> None:
        """Test that a formula record is joined against the formula table."""
        record = make_record("wget", kind=PackageKind.FORMULA)
        catalog = MockCatalog(formulae={"wget": make_formula_row("wget")})
        pkg = merge_one(record, catalog)
        assert pkg.name == "wget"
        assert pkg.kind == PackageKind.FORMULA
        assert pkg.desc == "retrieves files"

    def test_merge_one_dispatches_cask(self) -> None:
        """Test that a cask record is joined against the cask table."""
        record = make_record("firefox", kind=PackageKind.CASK, version="120.0")
        catalog = MockCatalog(casks={"firefox": make_cask_row("firefox")})
        pkg = merge_one(record, catalog)
        assert pkg.kind == PackageKind.CASK
        assert pkg.desc == "web browser"

    def test_merge_preserves_input_order(self) -> None:
        """Test that merge yields one Package per record in the same order."""
        records = [
            make_record("firefox", kind=PackageKind.CASK, version="120.0"),
            make_record("wget", kind=PackageKind.FORMULA),
            make_record("jq", kind=PackageKind.FORMULA, version="1.7"),
        ]
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget"), "jq": make_formula_row("jq")},
            casks={"firefox": make_cask_row("firefox")},
        )
        names = [p.name for p in merge(records, catalog)]
        assert names == ["firefox", "wget", "jq"]

    def test_merge_empty_records_yields_empty(self) -> None:
        """Test that merging no records yields an empty list."""
        assert merge([], MockCatalog()) == []


class TestMergeFormula:
    """Tests for formula record/row joining."""

    def test_no_row_falls_back_to_installed_only(self) -> None:
        """Test that a formula absent from the catalog still yields a Package.

        With no catalog row, desc and latest_version are unset and the
        installed version still populates the Package.
        """
        record = make_record("tapped", version="9.9")
        pkg = merge_one(record, MockCatalog())
        assert pkg.name == "tapped"
        assert pkg.desc is None
        assert pkg.metadata["latest_version"] is None
        assert pkg.versions == ["9.9"]

    def test_row_supplies_desc_and_latest(self) -> None:
        """Test that the catalog row supplies desc and the latest version."""
        record = make_record("wget", version="1.21.4")
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget", version="1.21.5")}
        )
        pkg = merge_one(record, catalog)
        assert pkg.desc == "retrieves files"
        assert pkg.metadata["latest_version"] == "1.21.5"

    def test_latest_includes_revision(self) -> None:
        """Test that a non-zero catalog revision is folded into latest_version."""
        record = make_record("wget", version="1.21.4")
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget", version="1.21.4", revision=2)}
        )
        pkg = merge_one(record, catalog)
        assert pkg.metadata["latest_version"] == "1.21.4.2"

    def test_keg_only_sets_flag_and_clears_not_linked(self) -> None:
        """Test that keg_only sets KEG_ONLY and clears NOT_LINKED.

        A keg-only formula is unlinked by design, so the unlinked-install
        signal must not be reported as a problem.
        """
        record = make_record("openssl", linked=False)
        catalog = MockCatalog(
            formulae={"openssl": make_formula_row("openssl", keg_only=True)}
        )
        pkg = merge_one(record, catalog)
        assert PackageStatus.KEG_ONLY in pkg.status
        assert PackageStatus.NOT_LINKED not in pkg.status

    def test_unlinked_non_keg_only_keeps_not_linked(self) -> None:
        """Test that an unlinked, non-keg-only formula keeps NOT_LINKED.

        This is the contrast case proving the keg-only branch is what clears
        the flag, not the merge in general.
        """
        record = make_record("wget", linked=False)
        catalog = MockCatalog(formulae={"wget": make_formula_row("wget")})
        pkg = merge_one(record, catalog)
        assert PackageStatus.NOT_LINKED in pkg.status

    def test_has_service_sets_flag(self) -> None:
        """Test that a row with has_service sets HAS_SERVICE."""
        record = make_record("syncthing")
        catalog = MockCatalog(
            formulae={"syncthing": make_formula_row("syncthing", has_service=True)}
        )
        pkg = merge_one(record, catalog)
        assert PackageStatus.HAS_SERVICE in pkg.status

    def test_outdated_set_when_versions_differ(self) -> None:
        """Test that a version mismatch against the catalog sets OUTDATED."""
        record = make_record("wget", version="1.21.4")
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget", version="1.21.5")}
        )
        pkg = merge_one(record, catalog)
        assert PackageStatus.OUTDATED in pkg.status

    def test_not_outdated_when_versions_match(self) -> None:
        """Test that a matching version does not set OUTDATED."""
        record = make_record("wget", version="1.21.4")
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget", version="1.21.4")}
        )
        pkg = merge_one(record, catalog)
        assert PackageStatus.OUTDATED not in pkg.status

    def test_tap_prefers_record_over_row(self) -> None:
        """Test that an installed record's tap overrides the catalog row's tap."""
        record = make_record("wget", tap="me/mytap")
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget", tap="homebrew/core")}
        )
        pkg = merge_one(record, catalog)
        assert pkg.tap == "me/mytap"

    def test_tap_falls_back_to_row(self) -> None:
        """Test that the row's tap is used when the record has none."""
        record = make_record("wget", tap=None)
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget", tap="homebrew/core")}
        )
        pkg = merge_one(record, catalog)
        assert pkg.tap == "homebrew/core"


class TestFormulaOutdated:
    """Tests for the outdated decision, exercised via merge_one.

    A HEAD install is never outdated; otherwise a higher catalog version,
    version_scheme, or revision marks the installed package outdated, while
    equal effective versions (and a lower catalog scheme) do not.
    """

    @pytest.mark.parametrize(
        ("record", "row", "expected"),
        [
            pytest.param(
                make_record("wget", version="1.21.4", head=True),
                make_formula_row("wget", version="9.9.9"),
                False,
                id="head_install_never_outdated",
            ),
            pytest.param(
                make_record("wget", version="1.21.4", version_scheme=0),
                make_formula_row("wget", version="1.21.4", version_scheme=1),
                True,
                id="higher_version_scheme_forces_outdated",
            ),
            pytest.param(
                make_record("wget", version="1.21.4", revision=0),
                make_formula_row("wget", version="1.21.4", revision=1),
                True,
                id="revision_bump_is_outdated",
            ),
            pytest.param(
                make_record("wget", version="1.21.4", revision=1),
                make_formula_row("wget", version="1.21.4", revision=1),
                False,
                id="equal_effective_versions_not_outdated",
            ),
            pytest.param(
                make_record("wget", version="1.21.4", version_scheme=2),
                make_formula_row("wget", version="1.21.4", version_scheme=1),
                False,
                id="lower_catalog_scheme_not_outdated",
            ),
        ],
    )
    def test_outdated_decision(self, record, row, expected) -> None:
        """Test the outdated decision logic."""
        catalog = MockCatalog(formulae={record.name: row})
        assert (PackageStatus.OUTDATED in merge_one(record, catalog).status) is expected


class TestMergeCask:
    """Tests for cask record/row joining."""

    def test_no_row_falls_back_to_installed_only(self) -> None:
        """Test that a cask absent from the catalog still yields a Package."""
        record = make_record("custom", kind=PackageKind.CASK, version="2.0")
        pkg = merge_one(record, MockCatalog())
        assert pkg.kind == PackageKind.CASK
        assert pkg.desc is None
        assert pkg.metadata["latest_version"] is None
        assert pkg.versions == ["2.0"]

    def test_row_supplies_desc_and_latest(self) -> None:
        """Test that the cask row supplies desc and latest version."""
        record = make_record("firefox", kind=PackageKind.CASK, version="119.0")
        catalog = MockCatalog(
            casks={"firefox": make_cask_row("firefox", version="120.0")}
        )
        pkg = merge_one(record, catalog)
        assert pkg.desc == "web browser"
        assert pkg.metadata["latest_version"] == "120.0"

    def test_tap_prefers_record_over_row(self) -> None:
        """Test that the installed record's tap overrides the cask row's tap."""
        record = make_record(
            "firefox", kind=PackageKind.CASK, version="120.0", tap="me/mytap"
        )
        catalog = MockCatalog(
            casks={"firefox": make_cask_row("firefox", tap="homebrew/cask")}
        )
        pkg = merge_one(record, catalog)
        assert pkg.tap == "me/mytap"


class TestCatalogInfo:
    """Tests for the catalog-only lookup path."""

    def test_resolves_alias_then_formula(self) -> None:
        """Test that the name is resolved through the alias table first."""
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget")},
            aliases={"wngt": "wget"},
        )
        pkg = catalog_info(catalog, "wngt")
        assert pkg is not None
        assert pkg.name == "wget"
        assert pkg.status == PackageStatus.NONE

    def test_falls_through_to_cask(self) -> None:
        """Test that a name absent from formulae resolves against casks."""
        catalog = MockCatalog(casks={"firefox": make_cask_row("firefox")})
        pkg = catalog_info(catalog, "firefox")
        assert pkg is not None
        assert pkg.kind == PackageKind.CASK

    def test_unknown_name_returns_none(self) -> None:
        """Test that a name unknown to the catalog returns None."""
        assert catalog_info(MockCatalog(), "nope") is None

    def test_formula_package_carries_catalog_deps(self) -> None:
        """Test that a catalog-only formula Package carries its catalog deps."""
        catalog = MockCatalog(
            formulae={"wget": make_formula_row("wget")},
            deps={"wget": ["openssl", "libidn2"]},
        )
        pkg = catalog_info(catalog, "wget")
        assert pkg is not None
        assert [d.name for d in pkg.deps] == ["openssl", "libidn2"]


class TestSearchPackages:
    """Tests for catalog search with installed-state enrichment."""

    def test_uninstalled_hit_is_catalog_only(self) -> None:
        """Test that a hit with no installed match is a catalog-only Package."""
        row = make_formula_row("wget")
        catalog = MockCatalog(search_results=[row])
        results = search_packages(catalog, "wget")
        assert len(results) == 1
        assert results[0].status == PackageStatus.NONE

    def test_installed_formula_hit_is_enriched(self) -> None:
        """Test that an installed formula hit returns the merged Package.

        The merged Package is returned by identity, not a fresh catalog-only one.
        """
        row = make_formula_row("wget")
        installed_pkg = merge_one(
            make_record("wget"), MockCatalog(formulae={"wget": row})
        )
        catalog = MockCatalog(search_results=[row])
        results = search_packages(catalog, "wget", installed={"wget": installed_pkg})
        assert results[0] is installed_pkg

    def test_installed_cask_hit_is_enriched_by_token(self) -> None:
        """Test that a cask hit is matched against installed by its token."""
        row = make_cask_row("firefox")
        installed_pkg = merge_one(
            make_record("firefox", kind=PackageKind.CASK, version="120.0"),
            MockCatalog(casks={"firefox": row}),
        )
        catalog = MockCatalog(search_results=[row])
        results = search_packages(catalog, "fire", installed={"firefox": installed_pkg})
        assert results[0] is installed_pkg

    def test_none_installed_treats_all_hits_as_uninstalled(self) -> None:
        """Test that None for installed treats every hit as uninstalled."""
        catalog = MockCatalog(
            search_results=[make_formula_row("wget"), make_cask_row("firefox")]
        )
        results = search_packages(catalog, "x", installed=None)
        assert all(p.status == PackageStatus.NONE for p in results)
        assert {p.name for p in results} == {"wget", "firefox"}
