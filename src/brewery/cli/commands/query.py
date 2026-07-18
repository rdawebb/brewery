"""Read-only query commands: list, info, search."""

from __future__ import annotations

from brewery.cli.context import _repository, app, console
from brewery.cli.error_formatting import command_error
from brewery.cli.output import spinner
from brewery.core.models import Package, PackageKind


@app.command(name="list", aliases=["l", "ls"])
@command_error()
def list_pkgs(
    kind: PackageKind | None = app.Option(
        None, "--kind", "-k", help="formula | cask | all"
    ),
    refresh: bool = app.Option(False, "--refresh", "-r", help="Refresh cache"),
    table: bool = app.Option(
        False, "--table", "-t", "--verbose", "-v", help="Show the full table view"
    ),
) -> None:
    """List packages in the repository.

    Args:
        kind: Filter by package kind.
        refresh: Refresh cache before listing packages.
        table: Show the full multi-column table instead of the compact view.
    """
    from brewery.cli.renderers import (
        _terminal_size,
        package_columns,
        package_table,
        paginate,
    )

    with _repository() as repo:
        pkgs: list[Package]

        if refresh:
            with spinner("Refreshing cache..."):
                repo.cache_mgr.invalidate()
                pkgs = repo.get_all_installed(kind_filter=kind)
        else:
            pkgs = repo.get_all_installed(kind_filter=kind)

        if not table:
            console.print(package_columns(pkgs, single_column=not console.is_terminal))
            return

        _, term_height = _terminal_size()
        page_size: int = max(term_height - 6, 1)  # header + footer buffer

        if len(pkgs) > page_size:
            paginate(pkgs=pkgs, page_size=page_size, console=console)

        else:
            console.print(package_table(pkgs), emoji=False)


@app.command(aliases=["i", "in"])
@command_error()
def info(
    name: str,
    kind: PackageKind | None = app.Option(
        None, "--kind", help="formula | cask | auto (default)"
    ),
) -> None:
    """Show detailed information about a package.

    Args:
        name: Name of the package.
        kind: Kind of the package (formula or cask). If not provided, will auto-detect.
    """
    from brewery.cli.renderers import package_details

    with _repository() as repo:
        pkg: Package = repo.get_details(name, kind)

        console.print(package_details(pkg))


@app.command(aliases=["s", "find"])
@command_error()
def search(
    term: str,
    table: bool = app.Option(
        False, "--table", "-t", "--verbose", "-v", help="Show the full table view"
    ),
) -> None:
    """Search for packages by name or description.

    Args:
        term: Search term.
        table: Show the full multi-column table instead of the compact view.
    """
    from brewery.cli.renderers import package_columns, package_table

    with _repository() as repo:
        pkgs: list[Package] = repo.search(term)

        if table:
            console.print(package_table(pkgs))
            return

        console.print(
            package_columns(
                pkgs, mark_installed=True, single_column=not console.is_terminal
            )
        )
