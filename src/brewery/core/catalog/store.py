"""SQLite-based catalog store for Homebrew formula and cask metadata."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

from brewery.core.config import ensure_cache_dir
from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

SCHEMA_VERSION = 1

_DEFAULT_DB_PATH: Path = ensure_cache_dir() / "catalog.db"

_SCHEMA: str = """
CREATE TABLE formula (
    name           TEXT PRIMARY KEY,
    desc           TEXT,
    homepage       TEXT,
    tap            TEXT,
    version        TEXT NOT NULL,
    revision       INTEGER NOT NULL DEFAULT 0,
    version_scheme INTEGER NOT NULL DEFAULT 0,
    keg_only       INTEGER NOT NULL DEFAULT 0,
    has_service    INTEGER NOT NULL DEFAULT 0,  -- formula ships a `service` block
    post_install   INTEGER NOT NULL DEFAULT 0,  -- gates a future `brew postinstall`
    bottle_url     TEXT,                        -- resolved for the current platform tag
    bottle_sha256  TEXT,
    bottle_cellar  TEXT,                        -- :any_skip_relocation | :any | <path>
    bottle_rebuild INTEGER NOT NULL DEFAULT 0,
    deprecated     INTEGER NOT NULL DEFAULT 0,
    disabled       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE cask (
    token        TEXT PRIMARY KEY,
    name         TEXT,
    desc         TEXT,
    homepage     TEXT,
    tap          TEXT,
    version      TEXT,            -- literal "latest" for version :latest casks
    sha256       TEXT,            -- NULL / "no_check" when unverifiable
    url          TEXT,
    auto_updates INTEGER NOT NULL DEFAULT 0,
    artifacts    TEXT,            -- JSON; enough to tell app vs pkg for later
    depends_on   TEXT,            -- JSON
    deprecated   INTEGER NOT NULL DEFAULT 0,
    disabled     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE deps (
    pkg  TEXT NOT NULL,
    dep  TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'runtime',
    PRIMARY KEY (pkg, dep, kind)
);
CREATE INDEX idx_deps_dep ON deps(dep);   -- reverse lookup: who needs X

CREATE TABLE alias (alias TEXT PRIMARY KEY, name TEXT NOT NULL);

CREATE VIRTUAL TABLE formula_fts
    USING fts5(name, desc, content='formula', content_rowid='rowid');

CREATE VIRTUAL TABLE cask_fts
    USING fts5(token, name, desc, content='cask', content_rowid='rowid');

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

_META_SCHEMA_VERSION_KEY = "schema_version"

_FORMULA_COLUMNS: tuple[str, ...] = (
    "name",
    "desc",
    "homepage",
    "tap",
    "version",
    "revision",
    "version_scheme",
    "keg_only",
    "has_service",
    "post_install",
    "bottle_url",
    "bottle_sha256",
    "bottle_cellar",
    "bottle_rebuild",
    "deprecated",
    "disabled",
)
_CASK_COLUMNS: tuple[str, ...] = (
    "token",
    "name",
    "desc",
    "homepage",
    "tap",
    "version",
    "sha256",
    "url",
    "auto_updates",
    "artifacts",
    "depends_on",
    "deprecated",
    "disabled",
)


def _upsert_sql(table: str, columns: tuple[str, ...]) -> str:
    """Build an ``INSERT OR REPLACE`` with named placeholders for a table.

    Args:
        table: The table name to insert into.
        columns: The columns to insert or replace.

    Returns:
        The SQL string with named placeholders.
    """
    cols: str = ", ".join(columns)
    named: str = ", ".join(f":{c}" for c in columns)

    return f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({named})"


@dataclass(frozen=True, slots=True)
class FormulaRow:
    """A single formula row from the catalog, with booleans already decoded."""

    name: str
    desc: str | None
    homepage: str | None
    tap: str | None
    version: str
    revision: int
    version_scheme: int
    keg_only: bool
    has_service: bool
    post_install: bool
    bottle_url: str | None
    bottle_sha256: str | None
    bottle_cellar: str | None
    bottle_rebuild: int
    deprecated: bool
    disabled: bool


@dataclass(frozen=True, slots=True)
class CaskRow:
    """A single cask row from the catalog, with JSON/booleans already decoded."""

    token: str
    name: str | None
    desc: str | None
    homepage: str | None
    tap: str | None
    version: str | None
    sha256: str | None
    url: str | None
    auto_updates: bool
    artifacts: Any | None  # parsed JSON (list/dict) or None
    depends_on: Any | None  # parsed JSON (dict) or None
    deprecated: bool
    disabled: bool


def _json_or_none(value: str | None) -> Any | None:
    """Decode a stored JSON TEXT column, tolerating NULL and malformed data.

    Args:
        value: The JSON string to decode, or None if the field is NULL.

    Returns:
        The parsed JSON value if valid, None otherwise.
    """
    if not value:
        return None

    try:
        return orjson.loads(value)

    except orjson.JSONDecodeError:
        log.warning(event="catalog_json_decode_failed", raw=value[:120])
        return None


def _formula_from_row(row: sqlite3.Row) -> FormulaRow:
    """Map a raw formula row to a typed FormulaRow.

    Args:
        row: The raw formula row from the database.

    Returns:
        The parsed formula row as a typed FormulaRow.
    """
    return FormulaRow(
        name=row["name"],
        desc=row["desc"],
        homepage=row["homepage"],
        tap=row["tap"],
        version=row["version"],
        revision=row["revision"],
        version_scheme=row["version_scheme"],
        keg_only=bool(row["keg_only"]),
        has_service=bool(row["has_service"]),
        post_install=bool(row["post_install"]),
        bottle_url=row["bottle_url"],
        bottle_sha256=row["bottle_sha256"],
        bottle_cellar=row["bottle_cellar"],
        bottle_rebuild=row["bottle_rebuild"],
        deprecated=bool(row["deprecated"]),
        disabled=bool(row["disabled"]),
    )


def _cask_from_row(row: sqlite3.Row) -> CaskRow:
    """Map a raw cask row to a typed CaskRow.

    Args:
        row: The raw cask row from the database.

    Returns:
        The parsed cask row as a typed CaskRow.
    """
    return CaskRow(
        token=row["token"],
        name=row["name"],
        desc=row["desc"],
        homepage=row["homepage"],
        tap=row["tap"],
        version=row["version"],
        sha256=row["sha256"],
        url=row["url"],
        auto_updates=bool(row["auto_updates"]),
        artifacts=_json_or_none(row["artifacts"]),
        depends_on=_json_or_none(row["depends_on"]),
        deprecated=bool(row["deprecated"]),
        disabled=bool(row["disabled"]),
    )


def _fts_match(query: str) -> str:
    """Build a safe FTS5 MATCH expression from free-text input.

    Each whitespace-separated token is wrapped as a quoted phrase with a prefix
    star, which neutralises FTS operator characters and supports partial typing.
    Embedded double quotes are dropped so the expression can never be malformed.

    Args:
        query: Raw user search text.

    Returns:
        An FTS5 MATCH string, or "" when there is nothing to search for.
    """
    tokens: list[str] = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"*' for t in tokens)


class Catalog:
    """Typed accessor over the Homebrew catalog sqlite store.

    Callers never touch SQL directly. The connection runs in WAL mode so a
    reader sees a consistent snapshot while daemon updates the contents in a single
    transaction.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """Open (creating if needed) the catalog database.

        Args:
            db_path: Override for the database location (useful for tests)
        """
        ensure_cache_dir()
        self.db_path: Path = db_path or _DEFAULT_DB_PATH
        self._conn: sqlite3.Connection = self._connect()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        """Open the connection and apply pragmas.

        Returns:
            A configured sqlite3 connection with a Row factory.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # Allows the daemon to write while readers stay on the previous snapshot
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")

        return conn

    def _ensure_schema(self) -> None:
        """Create the schema on a fresh database, or recreate on a version mismatch."""
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()

        if exists is None:
            self._create_schema()
            return

        found: int | None = self.schema_version()
        if found != SCHEMA_VERSION:
            log.warning(
                event="catalog_schema_version_mismatch",
                found=found,
                expected=SCHEMA_VERSION,
            )

            # Drop tables and recreate the schema on version mismatch
            self._drop_all()
            self._create_schema()

    def _drop_all(self) -> None:
        """Drop every catalog object so _create_schema can rebuild from clean."""
        objects = self._conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()

        with self._conn:
            for obj in objects:
                self._conn.execute(f'DROP {obj["type"]} IF EXISTS "{obj["name"]}"')

    def _create_schema(self) -> None:
        """Build all tables/indexes and stamp the schema version."""
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?)",
                (_META_SCHEMA_VERSION_KEY, str(SCHEMA_VERSION)),
            )

        log.info(
            event="catalog_schema_created",
            version=SCHEMA_VERSION,
            path=str(object=self.db_path),
        )

    def get_formula(self, name: str) -> FormulaRow | None:
        """Fetch a single formula by canonical name.

        Args:
            name: Canonical formula name (resolve aliases via the alias table
                first if the input may be an alias).

        Returns:
            The FormulaRow, or None if not present.
        """
        row = self._conn.execute(
            "SELECT * FROM formula WHERE name = ?", (name,)
        ).fetchone()

        return _formula_from_row(row) if row else None

    def get_formulae(self, names: list[str]) -> dict[str, FormulaRow]:
        """Batch fetch formulae, keyed by name.

        Args:
            names: Canonical formula names.

        Returns:
            Mapping of name to FormulaRow for those that exist. Missing names are ignored.
        """
        if not names:
            return {}

        placeholders: str = ",".join("?" * len(names))
        rows = self._conn.execute(
            f"SELECT * FROM formula WHERE name IN ({placeholders})", names
        ).fetchall()

        return {row["name"]: _formula_from_row(row) for row in rows}

    def get_cask(self, token: str) -> CaskRow | None:
        """Fetch a single cask by token.

        Args:
            token: Cask token (the canonical identifier).

        Returns:
            The CaskRow, or None if not present.
        """
        row = self._conn.execute(
            "SELECT * FROM cask WHERE token = ?", (token,)
        ).fetchone()

        return _cask_from_row(row) if row else None

    def get_casks(self, tokens: list[str]) -> dict[str, CaskRow]:
        """Batch fetch casks, keyed by name.

        Args:
            tokens: Cask tokens.

        Returns:
            Mapping of token to CaskRow for those that exist.
        """
        if not tokens:
            return {}

        placeholders: str = ",".join("?" * len(tokens))
        rows = self._conn.execute(
            f"SELECT * FROM cask WHERE token IN ({placeholders})", tokens
        ).fetchall()

        return {row["token"]: _cask_from_row(row) for row in rows}

    def deps_of(self, name: str) -> list[str]:
        """Return the direct dependency names of a formula.

        Args:
            name: Formula name.

        Returns:
            Sorted list of dependency names (empty if none/unknown).
        """
        rows = self._conn.execute(
            "SELECT DISTINCT dep FROM deps WHERE pkg = ? ORDER BY dep", (name,)
        ).fetchall()

        return [row["dep"] for row in rows]

    def runtime_deps(self, name: str) -> list[str]:
        """Return the runtime dependency names of a formula.

        Args:
            name: Formula name.

        Returns:
            Sorted list of runtime dependency names (empty if none/unknown).
        """
        rows = self._conn.execute(
            "SELECT DISTINCT dep FROM deps WHERE pkg = ? AND kind = 'runtime' ORDER BY dep",
            (name,),
        ).fetchall()

        return [row["dep"] for row in rows]

    def used_by(self, name: str) -> list[str]:
        """Return the names of formulae that depend on the given package.

        Uses the reverse index on deps(dep). This is the catalog view of
        reverse dependencies (every dependent, installed or not).

        Args:
            name: Dependency name to look up dependents for.

        Returns:
            Sorted list of dependent package names.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT pkg FROM deps WHERE dep = ? ORDER BY pkg", (name,)
        ).fetchall()

        return [row["pkg"] for row in rows]

    def resolve_alias(self, name: str) -> str:
        """Resolve a possible alias to its canonical formula name.

        Args:
            name: A formula name or alias.

        Returns:
            The canonical name if an alias mapping exists, otherwise the input
            unchanged.
        """
        row = self._conn.execute(
            "SELECT name FROM alias WHERE alias = ?", (name,)
        ).fetchone()

        return row["name"] if row else name

    def aliases_of(self, name: str) -> list[str]:
        """List all aliases of a formula name

        Args:
            name: A formula name.

        Returns:
            A list of aliases for a given formula name (empty if None)
        """
        rows = self._conn.execute(
            "SELECT alias FROM alias WHERE name = ? ORDER BY alias", (name,)
        ).fetchall()

        return [row["alias"] for row in rows]

    def search(self, query: str, limit: int = 50) -> list[FormulaRow | CaskRow]:
        """Full-text search over formula and cask name and description.

        Both FTS tables are queried and the results concatenated, formulae
        first then casks. Relevance rank is per-table in FTS5 and not comparable
        across tables, so this does not attempt a global ranking. The total is
        capped at 'limit'.

        Args:
            query: Free-text search string.
            limit: Maximum number of results across both kinds.

        Returns:
            Formula and cask rows matching the query. Empty when the query has
            no usable tokens.
        """
        match: str = _fts_match(query)
        if not match:
            return []

        formula_rows = self._conn.execute(
            "SELECT f.* FROM formula_fts "
            "JOIN formula f ON f.rowid = formula_fts.rowid "
            "WHERE formula_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()

        results: list[FormulaRow | CaskRow] = [
            _formula_from_row(row) for row in formula_rows
        ]

        remaining: int = limit - len(results)
        if remaining > 0:
            cask_rows = self._conn.execute(
                "SELECT c.* FROM cask_fts "
                "JOIN cask c ON c.rowid = cask_fts.rowid "
                "WHERE cask_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (match, remaining),
            ).fetchall()
            results.extend(_cask_from_row(row) for row in cask_rows)

        return results

    def write_formulae(
        self,
        formulae: list[dict[str, Any]],
        deps: list[dict[str, Any]],
        aliases: list[dict[str, Any]],
    ) -> None:
        """Replace the formula catalog in one atomic transaction and rebuild FTS.

        Args:
            formulae: Formula column dicts (keys = formula columns).
            deps: Dep edge dicts with keys ``pkg``, ``dep``, ``kind``.
            aliases: Alias dicts with keys ``alias``, ``name``.
        """
        formula_sql: str = _upsert_sql("formula", _FORMULA_COLUMNS)
        with self._conn:
            self._conn.executemany(formula_sql, formulae)
            self._conn.execute("DELETE FROM deps")
            self._conn.executemany(
                "INSERT OR IGNORE INTO deps (pkg, dep, kind) "
                "VALUES (:pkg, :dep, :kind)",
                deps,
            )
            self._conn.execute("DELETE FROM alias")
            self._conn.executemany(
                "INSERT OR REPLACE INTO alias (alias, name) VALUES (:alias, :name)",
                aliases,
            )
            self._conn.execute("INSERT INTO formula_fts(formula_fts) VALUES('rebuild')")

        log.info(
            event="catalog_formulae_written",
            formulae=len(formulae),
            deps=len(deps),
            aliases=len(aliases),
        )

    def write_casks(self, casks: list[dict[str, Any]]) -> None:
        """Replace the cask catalog in one atomic transaction and rebuild FTS.

        Args:
            casks: Cask column dicts (keys = cask columns).
        """
        cask_sql: str = _upsert_sql("cask", _CASK_COLUMNS)
        with self._conn:
            self._conn.executemany(cask_sql, casks)
            self._conn.execute("INSERT INTO cask_fts(cask_fts) VALUES('rebuild')")

        log.info(event="catalog_casks_written", casks=len(casks))

    def get_meta(self, key: str) -> str | None:
        """Read a value from the meta table.

        Args:
            key: Meta key (e.g. "etag", "last_modified", "fetched_at").

        Returns:
            The stored value, or None if the key is absent.
        """
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()

        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a value into the meta table.

        Args:
            key: Meta key.
            value: Value to store.
        """
        with self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def schema_version(self) -> int | None:
        """Return the stored schema version, or None if unset/unparseable.

        Returns:
            The schema version as an integer, or None if not set or unparseable.
        """
        raw: str | None = self.get_meta(_META_SCHEMA_VERSION_KEY)
        if raw is None:
            return None

        try:
            return int(raw)

        except ValueError:
            log.warning(event="catalog_schema_version_unparseable", raw=raw)
            return None

    def close(self) -> None:
        """Checkpoint the WAL and close the underlying connection."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        except sqlite3.Error as e:
            log.debug(event="catalog_checkpoint_failed", error=str(object=e))

        self._conn.close()

    def __enter__(self) -> Catalog:
        """Context manager entry point.

        Returns:
            The catalog instance.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Context manager exit point.

        Args:
            exc: Exception details, if any.
        """
        self.close()
