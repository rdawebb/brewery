"""Integration tests for the SQLite catalog store (real DB, WAL, FTS5)."""

from __future__ import annotations

from typing import Any, Generator

import pytest

from brewery.core.catalog import (
    SCHEMA_VERSION,
    CaskRow,
    Catalog,
    FormulaRow,
)

pytestmark = pytest.mark.integration


def formula_dict(name: str, **overrides: Any) -> dict[str, Any]:
    """Build a full-column formula row dict, overridable per field."""
    base = {
        "name": name,
        "desc": f"{name} description",
        "homepage": f"https://example/{name}",
        "tap": "homebrew/core",
        "version": "1.0.0",
        "revision": 0,
        "version_scheme": 0,
        "keg_only": 0,
        "has_service": 0,
        "post_install": 0,
        "bottle_url": None,
        "bottle_sha256": None,
        "bottle_cellar": None,
        "bottle_rebuild": 0,
        "deprecated": 0,
        "disabled": 0,
    }
    base.update(overrides)
    return base


def cask_dict(token: str, **overrides: Any) -> dict[str, Any]:
    """Build a full-column cask row dict, overridable per field."""
    base = {
        "token": token,
        "name": token.title(),
        "desc": f"{token} description",
        "homepage": f"https://example/{token}",
        "tap": "homebrew/cask",
        "version": "1.0.0",
        "sha256": None,
        "url": None,
        "auto_updates": 0,
        "artifacts": None,
        "depends_on": None,
        "deprecated": 0,
        "disabled": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def empty_catalog(tmp_path) -> Generator[Catalog, None, None]:
    """A fresh Catalog backed by an isolated temp database file."""
    cat = Catalog(db_path=tmp_path / "catalog.db")
    yield cat
    cat.close()


class TestSchema:
    """Tests for schema creation and version handling."""

    def test_fresh_db_stamps_schema_version(self, empty_catalog) -> None:
        """Test that a brand-new database is stamped with the current version."""
        assert empty_catalog.schema_version() == SCHEMA_VERSION

    def test_reopen_preserves_data(self, tmp_path) -> None:
        """Test that data written then reopened at the same path survives."""
        path = tmp_path / "catalog.db"
        cat = Catalog(db_path=path)
        cat.write_formulae([formula_dict("wget")], [], [])
        cat.close()

        reopened = Catalog(db_path=path)
        try:
            assert reopened.get_formula("wget") is not None
            assert reopened.schema_version() == SCHEMA_VERSION
        finally:
            reopened.close()

    def test_version_mismatch_rebuilds(self, tmp_path) -> None:
        """Test that a stale schema version triggers a drop-and-rebuild.

        Data from the old schema must not survive the rebuild.
        """
        path = tmp_path / "catalog.db"
        cat = Catalog(db_path=path)
        cat.write_formulae([formula_dict("wget")], [], [])
        # Simulate an older schema by rewriting the stored version
        cat.set_meta("schema_version", str(SCHEMA_VERSION + 1))
        cat.close()

        reopened = Catalog(db_path=path)
        try:
            assert reopened.schema_version() == SCHEMA_VERSION
            assert reopened.get_formula("wget") is None
        finally:
            reopened.close()

    def test_unparseable_version_is_none(self, empty_catalog) -> None:
        """Test that a non-integer stored version reads back as None."""
        empty_catalog.set_meta("schema_version", "not-a-number")
        assert empty_catalog.schema_version() is None


class TestFormulaReadWrite:
    """Tests for formula write and read paths."""

    def test_round_trip_all_fields(self, empty_catalog) -> None:
        """Test that a fully-populated formula round-trips through the DB."""
        empty_catalog.write_formulae(
            [
                formula_dict(
                    "openssl",
                    desc="crypto",
                    tap="homebrew/core",
                    version="3.2.1",
                    revision=2,
                    version_scheme=1,
                    keg_only=1,
                    has_service=0,
                    deprecated=1,
                )
            ],
            [],
            [],
        )
        row = empty_catalog.get_formula("openssl")
        assert row == FormulaRow(
            name="openssl",
            desc="crypto",
            homepage="https://example/openssl",
            tap="homebrew/core",
            version="3.2.1",
            revision=2,
            version_scheme=1,
            keg_only=True,
            has_service=False,
            post_install=False,
            bottle_url=None,
            bottle_sha256=None,
            bottle_cellar=None,
            bottle_rebuild=0,
            deprecated=True,
            disabled=False,
        )

    def test_int_columns_become_bools(self, empty_catalog) -> None:
        """Test that integer flag columns are exposed as Python bools."""
        empty_catalog.write_formulae(
            [formula_dict("x", keg_only=1, has_service=1, post_install=1)], [], []
        )
        row = empty_catalog.get_formula("x")
        assert row.keg_only is True
        assert row.has_service is True
        assert row.post_install is True

    def test_missing_formula_returns_none(self, empty_catalog) -> None:
        """Test that an absent formula returns None."""
        assert empty_catalog.get_formula("nope") is None

    def test_write_replaces_existing_row(self, empty_catalog) -> None:
        """Test that re-writing the same name upserts rather than duplicates."""
        empty_catalog.write_formulae([formula_dict("wget", version="1.0")], [], [])
        empty_catalog.write_formulae([formula_dict("wget", version="2.0")], [], [])
        assert empty_catalog.get_formula("wget").version == "2.0"

    def test_get_formulae_batch(self, empty_catalog) -> None:
        """Test that batch fetch returns a name-keyed mapping of present rows."""
        empty_catalog.write_formulae(
            [formula_dict("a"), formula_dict("b"), formula_dict("c")], [], []
        )
        result = empty_catalog.get_formulae(["a", "c", "missing"])
        assert set(result) == {"a", "c"}
        assert result["a"].name == "a"

    def test_get_formulae_empty_list(self, empty_catalog) -> None:
        """Test that an empty name list yields an empty mapping without querying."""
        assert empty_catalog.get_formulae([]) == {}


class TestCaskReadWrite:
    """Tests for cask write and read paths."""

    def test_round_trip(self, empty_catalog) -> None:
        """Test that a cask round-trips through the DB."""
        empty_catalog.write_casks(
            [cask_dict("firefox", name="Firefox", version="120.0", auto_updates=1)]
        )
        row = empty_catalog.get_cask("firefox")
        assert isinstance(row, CaskRow)
        assert row.token == "firefox"
        assert row.name == "Firefox"
        assert row.version == "120.0"
        assert row.auto_updates is True

    def test_json_columns_decoded(self, empty_catalog) -> None:
        """Test that JSON-text columns are decoded back into structures."""
        empty_catalog.write_casks(
            [
                cask_dict(
                    "firefox",
                    artifacts='[{"app": "Firefox.app"}]',
                    depends_on='{"macos": ">= 11"}',
                )
            ]
        )
        row = empty_catalog.get_cask("firefox")
        assert row.artifacts == [{"app": "Firefox.app"}]
        assert row.depends_on == {"macos": ">= 11"}

    def test_missing_cask_returns_none(self, empty_catalog) -> None:
        """Test that an absent cask returns None."""
        assert empty_catalog.get_cask("nope") is None

    def test_get_casks_batch(self, empty_catalog) -> None:
        """Test that batch cask fetch returns a token-keyed mapping."""
        empty_catalog.write_casks([cask_dict("a"), cask_dict("b")])
        result = empty_catalog.get_casks(["a", "missing"])
        assert set(result) == {"a"}


class TestDepsAndAliases:
    """Tests for dependency edges and alias resolution."""

    def _write_graph(self, cat: Catalog) -> None:
        cat.write_formulae(
            [formula_dict("curl"), formula_dict("openssl"), formula_dict("ca-certs")],
            [
                {"pkg": "curl", "dep": "openssl", "kind": "runtime"},
                {"pkg": "curl", "dep": "ca-certs", "kind": "runtime"},
                {"pkg": "openssl", "dep": "ca-certs", "kind": "runtime"},
            ],
            [{"alias": "curl-ssl", "name": "curl"}],
        )

    def test_deps_of_sorted(self, empty_catalog) -> None:
        """Test that direct dependencies are returned sorted."""
        self._write_graph(empty_catalog)
        assert empty_catalog.deps_of("curl") == ["ca-certs", "openssl"]

    def test_deps_of_none(self, empty_catalog) -> None:
        """Test that a formula with no deps returns an empty list."""
        self._write_graph(empty_catalog)
        assert empty_catalog.deps_of("ca-certs") == []

    def test_used_by_reverse_index(self, empty_catalog) -> None:
        """Test that reverse dependents are found via the dep index."""
        self._write_graph(empty_catalog)
        assert empty_catalog.used_by("ca-certs") == ["curl", "openssl"]

    def test_resolve_alias_hit(self, empty_catalog) -> None:
        """Test that a known alias resolves to its canonical name."""
        self._write_graph(empty_catalog)
        assert empty_catalog.resolve_alias("curl-ssl") == "curl"

    def test_resolve_alias_passthrough(self, empty_catalog) -> None:
        """Test that an unknown name is returned unchanged."""
        self._write_graph(empty_catalog)
        assert empty_catalog.resolve_alias("curl") == "curl"

    def test_write_formulae_clears_old_deps(self, empty_catalog) -> None:
        """Test that re-writing formulae replaces the dependency edges wholesale.

        write_formulae DELETEs deps before re-inserting, so stale edges must not
        survive a second write that omits them.
        """
        self._write_graph(empty_catalog)
        empty_catalog.write_formulae([formula_dict("curl")], [], [])
        assert empty_catalog.deps_of("curl") == []

    def test_write_formulae_clears_old_aliases(self, empty_catalog) -> None:
        """Test that re-writing formulae replaces the alias table wholesale."""
        self._write_graph(empty_catalog)
        empty_catalog.write_formulae([formula_dict("curl")], [], [])
        assert empty_catalog.resolve_alias("curl-ssl") == "curl-ssl"


class TestSearch:
    """Tests for full-text search across formula and cask tables."""

    def _populate(self, cat: Catalog) -> None:
        cat.write_formulae(
            [
                formula_dict("wget", desc="internet file retriever"),
                formula_dict("curl", desc="transfer data with URLs"),
            ],
            [],
            [],
        )
        cat.write_casks([cask_dict("firefox", desc="web browser internet")])

    def test_match_by_name(self, empty_catalog) -> None:
        """Test that a name token matches a formula."""
        self._populate(empty_catalog)
        names = [r.name for r in empty_catalog.search("wget")]
        assert "wget" in names

    def test_match_by_desc(self, empty_catalog) -> None:
        """Test that a description token matches."""
        self._populate(empty_catalog)
        results = empty_catalog.search("retriever")
        assert any(getattr(r, "name", None) == "wget" for r in results)

    def test_prefix_match(self, empty_catalog) -> None:
        """Test that partial tokens match via the prefix-star expression."""
        self._populate(empty_catalog)
        names = [r.name for r in empty_catalog.search("wge")]
        assert "wget" in names

    def test_formulae_before_casks(self, empty_catalog) -> None:
        """Test that formula hits are ordered before cask hits.

        A query that matches both a formula desc and a cask desc returns the
        formula first, since the two FTS tables are concatenated formula-first.
        """
        self._populate(empty_catalog)
        results = empty_catalog.search("internet")
        kinds = [type(r).__name__ for r in results]
        assert kinds.index("FormulaRow") < kinds.index("CaskRow")

    def test_cask_matched(self, empty_catalog) -> None:
        """Test that a cask is found via its description."""
        self._populate(empty_catalog)
        tokens = [r.token for r in empty_catalog.search("browser")]
        assert "firefox" in tokens

    def test_empty_query_returns_empty(self, empty_catalog) -> None:
        """Test that a query with no usable tokens returns nothing."""
        self._populate(empty_catalog)
        assert empty_catalog.search("   ") == []

    def test_quote_only_query_returns_empty(self, empty_catalog) -> None:
        """Test that a query of only quote characters yields no tokens."""
        self._populate(empty_catalog)
        assert empty_catalog.search('"""') == []

    def test_limit_caps_results(self, empty_catalog) -> None:
        """Test that the limit caps the total number of results."""
        empty_catalog.write_formulae(
            [formula_dict(f"tool{i}", desc="shared keyword") for i in range(5)],
            [],
            [],
        )
        assert len(empty_catalog.search("keyword", limit=3)) == 3

    def test_no_match_returns_empty(self, empty_catalog) -> None:
        """Test that a query matching nothing returns an empty list."""
        self._populate(empty_catalog)
        assert empty_catalog.search("zzzznomatch") == []

    def test_new_write_is_searchable(self, empty_catalog) -> None:
        """Test that a formula added in a later write becomes searchable.

        write_formulae rebuilds the FTS index, so newly-written rows match.
        """
        empty_catalog.write_formulae([formula_dict("oldname")], [], [])
        empty_catalog.write_formulae([formula_dict("newname")], [], [])
        assert [r.name for r in empty_catalog.search("newname")] == ["newname"]

    def test_omitted_rows_are_not_pruned(self, empty_catalog) -> None:
        """Test that a write omitting a previously-written formula leaves it intact."""
        empty_catalog.write_formulae([formula_dict("oldname")], [], [])
        empty_catalog.write_formulae([formula_dict("newname")], [], [])
        assert empty_catalog.get_formula("oldname") is not None
        assert [r.name for r in empty_catalog.search("oldname")] == ["oldname"]


class TestMetaAndLifecycle:
    """Tests for the meta key-value table and context-manager use."""

    def test_meta_upsert(self, empty_catalog) -> None:
        """Test that set_meta inserts then updates the same key."""
        empty_catalog.set_meta("etag", "abc")
        assert empty_catalog.get_meta("etag") == "abc"
        empty_catalog.set_meta("etag", "def")
        assert empty_catalog.get_meta("etag") == "def"

    def test_meta_missing_returns_none(self, empty_catalog) -> None:
        """Test that an unset meta key returns None."""
        assert empty_catalog.get_meta("absent") is None

    def test_context_manager_closes(self, tmp_path) -> None:
        """Test that the context manager yields a usable catalog and closes it."""
        with Catalog(db_path=tmp_path / "c.db") as cat:
            cat.write_formulae([formula_dict("wget")], [], [])
            assert cat.get_formula("wget") is not None
        # After exit the connection is closed, so further use raises
        with pytest.raises(Exception):
            cat.get_formula("wget")
