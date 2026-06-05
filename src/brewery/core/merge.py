"""Merge-on-read join of installed state and catalog for up-to-date package status."""

from __future__ import annotations

from brewery.analysis.status import derive_local_status
from brewery.core.catalog import CaskRow, Catalog, FormulaRow
from brewery.core.fs_state import InstalledRecord
from brewery.core.models import (
    Dependency,
    Package,
    PackageKind,
    PackageStatus,
    effective_version,
)


def merge_one(record: InstalledRecord, catalog: Catalog) -> Package:
    """Join a single installed record against the catalog into a Package.

    Args:
        record: The installed record to merge.
        catalog: The catalog store.

    Returns:
        The merged Package.
    """
    if record.kind == PackageKind.FORMULA:
        return _merge_formula(record=record, row=catalog.get_formula(record.name))

    return _merge_cask(record=record, row=catalog.get_cask(record.name))


def merge(records: list[InstalledRecord], catalog: Catalog) -> list[Package]:
    """Join installed records against the catalog into Package objects.

    A record with no catalog row (e.g. a tapped formula absent from the core catalog)
    still yields a Package built from installed state alone.

    Args:
        records: Installed records from the filesystem scan.
        catalog: The catalog store.

    Returns:
        One Package per record, in the same order.
    """
    formula_rows: dict[str, FormulaRow] = catalog.get_formulae(
        [r.name for r in records if r.kind == PackageKind.FORMULA]
    )
    cask_rows: dict[str, CaskRow] = catalog.get_casks(
        [r.name for r in records if r.kind == PackageKind.CASK]
    )

    packages: list[Package] = []
    for record in records:
        if record.kind == PackageKind.FORMULA:
            packages.append(
                _merge_formula(record=record, row=formula_rows.get(record.name))
            )
        else:
            packages.append(_merge_cask(record=record, row=cask_rows.get(record.name)))

    return packages


def _merge_formula(record: InstalledRecord, row: FormulaRow | None) -> Package:
    """Build a formula Package from its record and catalog row (if any).

    Args:
        record: The installed record.
        row: The catalog row for this formula, if any.

    Returns:
        The merged formula Package.
    """
    status: PackageStatus = derive_local_status(
        kind=PackageKind.FORMULA,
        head=record.head,
        linked=record.linked,
        pinned=record.pinned,
    )

    desc: str | None = None
    latest: str | None = None
    tap: str | None = record.tap

    if row is not None:
        desc = row.desc
        tap = record.tap or row.tap
        latest = effective_version(version=row.version, revision=row.revision)
        if row.keg_only:
            status |= PackageStatus.KEG_ONLY
            # Keg-only formulae are unlinked by design
            status &= ~PackageStatus.NOT_LINKED
        if row.has_service:
            status |= PackageStatus.HAS_SERVICE
        if _formula_outdated(record=record, row=row):
            status |= PackageStatus.OUTDATED

    return _package(record=record, desc=desc, latest=latest, status=status, tap=tap)


def _merge_cask(record: InstalledRecord, row: CaskRow | None) -> Package:
    """Build a cask Package from its record and catalog row (if any).

    Args:
        record: The installed record.
        row: The catalog row for this cask, if any.

    Returns:
        The merged cask Package.
    """
    status: PackageStatus = derive_local_status(
        kind=PackageKind.CASK,
        head=record.head,
        linked=record.linked,
        pinned=record.pinned,
    )

    desc: str | None = row.desc if row else None
    latest: str | None = row.version if row else None
    tap: str | None = record.tap or (row.tap if row else None)

    return _package(record=record, desc=desc, latest=latest, status=status, tap=tap)


def _formula_outdated(record: InstalledRecord, row: FormulaRow) -> bool:
    """Whether an installed formula is outdated against the catalog.

    Inequality of effective versions, skipping HEAD installs.

    Args:
        record: The installed record.
        row: The catalog row for this formula.

    Returns:
        True if the formula is outdated, False otherwise.
    """
    if record.head:
        return False

    if record.version_scheme is not None and row.version_scheme > record.version_scheme:
        return True

    installed: str = effective_version(version=record.version, revision=record.revision)
    latest: str = effective_version(version=row.version, revision=row.revision)

    return bool(latest) and installed != latest


def _package(
    record: InstalledRecord,
    *,
    desc: str | None,
    latest: str | None,
    status: PackageStatus,
    tap: str | None,
) -> Package:
    """Assemble a Package from a record plus the merged catalog fields.

    Args:
        record: The installed record.
        desc: The package description.
        latest: The latest version from the catalog.
        status: The package status flags.
        tap: The tap name, if any.

    Returns:
        The assembled Package.
    """
    installed: str = effective_version(version=record.version, revision=record.revision)

    return Package(
        name=record.name,
        kind=record.kind,
        versions=[installed] if installed else [],
        desc=desc,
        status=status,
        installed_on=record.installed_on,
        size_kb=record.size_kb,
        deps=[Dependency(name=d) for d in record.deps],
        used_by=record.used_by,
        tap=tap,
        path=record.path,
        metadata={"latest_version": latest},
    )


def catalog_info(catalog: Catalog, name: str) -> Package | None:
    """Build a catalog-only Package for a package.

    Resolves the input through the alias table first, then formula table, then cask table.
    The result carries catalog data only.

    Args:
        catalog: The catalog store.
        name: User-supplied package name, alias, or cask token.

    Returns:
        A catalog-only Package, or None if the name is unknown to the catalog.
    """
    canonical: str = catalog.resolve_alias(name)

    formula_row: FormulaRow | None = catalog.get_formula(canonical)
    if formula_row is not None:
        return _catalog_formula_package(catalog=catalog, row=formula_row)

    cask_row: CaskRow | None = catalog.get_cask(canonical)
    if cask_row is not None:
        return _catalog_cask_package(row=cask_row)

    return None


def search_packages(
    catalog: Catalog, query: str, installed: dict[str, Package] | None = None
) -> list[Package]:
    """Search the whole catalog and enrich results that are installed.

    Runs the catalog FTS over name/description across all formulae and casks,
    then for each hit returns the installed (merged) Package when one exists,
    otherwise a catalog-only Package.

    Args:
        catalog: The catalog store.
        query: Free-text search query.
        installed: Map of installed package name -> merged Package, used to
            enrich/mark results. Empty/None means treat all hits as uninstalled.

    Returns:
        Packages for the search hits, installed ones enriched.
    """
    installed = installed or {}
    results: list[Package] = []

    for row in catalog.search(query):
        if isinstance(row, FormulaRow):
            if row.name in installed:
                results.append(installed[row.name])
            else:
                results.append(_catalog_formula_package(catalog=catalog, row=row))

        else:  # CaskRow
            if row.token in installed:
                results.append(installed[row.token])
            else:
                results.append(_catalog_cask_package(row=row))

    return results


def _catalog_formula_package(catalog: Catalog, row: FormulaRow) -> Package:
    """Catalog-only formula Package: status NONE, latest version, catalog deps.

    Args:
        catalog: The catalog store.
        row: The formula row from the catalog.

    Returns:
        A catalog-only formula Package.
    """
    latest: str = effective_version(version=row.version, revision=row.revision)

    return Package(
        name=row.name,
        kind=PackageKind.FORMULA,
        versions=[latest] if latest else [],
        desc=row.desc,
        status=PackageStatus.NONE,
        deps=[Dependency(name=d) for d in catalog.deps_of(row.name)],
        tap=row.tap,
        metadata={"latest_version": latest},
    )


def _catalog_cask_package(row: CaskRow) -> Package:
    """Catalog-only cask Package: status NONE, latest version, no deps.

    Args:
        row: The cask row from the catalog.

    Returns:
        A catalog-only cask Package.
    """
    return Package(
        name=row.token,
        kind=PackageKind.CASK,
        versions=[row.version] if row.version else [],
        desc=row.desc,
        status=PackageStatus.NONE,
        tap=row.tap,
        metadata={"latest_version": row.version},
    )
