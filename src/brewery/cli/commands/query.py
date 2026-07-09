"""Read-only query commands: list, info, search."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console
from brewery.cli.error_formatting import handle_error
from brewery.core.models import Package, PackageKind


@app.command(name="list", aliases=["ls", "l"])
def list_pkgs(
    kind: PackageKind | None = app.Option(
        None, "--kind", "-k", help="formula | cask | all"
    ),
    refresh: bool = app.Option(False, "--refresh", "-r", help="Refresh cache"),
) -> None:
    """List packages in the repository.

    Args:
        kind: Filter by package kind.
        refresh: Refresh cache before listing packages.
    """
    from brewery.cli.renderers import _terminal_size, package_table, paginate

    try:
        with _repository() as repo:
            pkgs: list[Package]

            if refresh:
                with console.status(
                    status="[bold yellow]Refreshing cache...[/bold yellow]",
                    refresh_per_second=5,
                ):
                    repo.cache_mgr.invalidate()
                    pkgs = repo.get_all_installed(kind_filter=kind)
            else:
                pkgs = repo.get_all_installed(kind_filter=kind)

            _, term_height = _terminal_size()
            page_size: int = term_height - 6  # header + footer buffer

            if len(pkgs) > page_size:
                paginate(pkgs=pkgs, page_size=page_size, console=console)
            else:
                console.print(package_table(pkgs), emoji=False)

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command(aliases=["i", "in"])
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

    try:
        with _repository() as repo:
            pkg: Package = repo.get_details(name, kind)

            console.print(package_details(pkg))

    except Exception as e:
        sys.exit(handle_error(error=e))


@app.command(aliases=["s", "find"])
def search(term: str) -> None:
    """Search for packages by name or description.

    Args:
        term: Search term.
    """
    try:
        with _repository() as repo:
            pkgs: list[Package] = repo.search(term)
            from brewery.cli.renderers import package_table

            console.print(package_table(pkgs))

    except Exception as e:
        sys.exit(handle_error(error=e))
