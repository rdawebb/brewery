"""Renderers for displaying package information in the CLI using Rich."""

from __future__ import annotations

import json
import shutil
from typing import Any, Iterable

import readchar
from rich import box
from rich.console import Console
from rich.table import Table

from brewery.core.cache import WIDTHS_CACHE
from brewery.core.config import ensure_cache_dir
from brewery.core.models import Package, PackageStatus

STATUS_LABELS: dict[PackageStatus, str] = {
    PackageStatus.OUTDATED: "[red]Outdated[/red]",
    PackageStatus.PINNED: "[yellow]Pinned[/yellow]",
    PackageStatus.NOT_LINKED: "[blue]Not Linked[/blue]",
    PackageStatus.KEG_ONLY: "[magenta]Keg-Only[/magenta]",
    PackageStatus.HEAD: "[cyan]HEAD[/cyan]",
    PackageStatus.HAS_SERVICE: "[green]Service[/green]",
}

COLUMN_DEFINITIONS: list[dict] = [
    dict(header="Kind"),
    dict(header="Name", style="bold"),
    dict(header="Installed"),
    dict(header="Latest"),
    dict(header="Status"),
    dict(header="Size (MB)", justify="right"),
    dict(header="Installed On", style="dim"),
]

# Terminal width mapped to column headers
_width_cache: dict[int, tuple[int, ...]] = {}
_width_cache_loaded = False


def _load_width_cache() -> None:
    """Load pre-computed column widths from cache."""
    try:
        if WIDTHS_CACHE.exists():
            data: Any = json.loads(WIDTHS_CACHE.read_text())
            _width_cache.update({int(k): tuple(v) for k, v in data.items()})

    except Exception:
        pass


def _ensure_width_cache_loaded() -> None:
    """Ensure the width cache is loaded from disk."""
    global _width_cache_loaded

    if not _width_cache_loaded:
        _load_width_cache()
        _width_cache_loaded = True


class _MeasuringTable(Table):
    """Table subclass that captures resolved column widths after layout.

    This class extends the functionality of the base Table class by storing
    the widths of columns after they have been calculated.
    """

    def __init__(self, *args, **kwargs):
        """Initialises Table class with additional resolved_widths attribute.

        Args:
            *args: Variable length argument list for the parent class.
            **kwargs: Keyword arguments for the parent class.
        """
        super().__init__(*args, **kwargs)
        self.resolved_widths: tuple[int, ...] | None = None

    def _calculate_column_widths(self, console, options) -> list[int]:
        """alculates and returns the widths of the table's columns.

        Overrides the parent class's method to capture the resolved widths
        after layout.

        Args:
            console: The console instance used for rendering the table.
            options: Additional options that may affect width calculations.

        Returns:
            list[int]: A list of calculated widths for each column.
        """
        widths: list[int] = super()._calculate_column_widths(console, options)
        self.resolved_widths: tuple[int, ...] = tuple(widths)

        return widths


def _terminal_size() -> tuple[int, int]:
    """Get current terminal size with sensible fallback

    Returns:
        Terminal width and height, or sensible fallback values
    """
    _size: tuple[int, int] = shutil.get_terminal_size(fallback=(120, 24))

    return _size.columns, _size.lines


def _build_table(widths: tuple[int, ...] | None = None) -> Table:
    """Construct the base table, injecting pre-computed widths if available.

    Args:
        widths: Pre-computed column widths.

    Returns:
        The base table object.
    """
    table = Table(box=box.MINIMAL_HEAVY_HEAD)

    for i, col in enumerate(iterable=COLUMN_DEFINITIONS):
        col: dict = dict(col)
        if widths is not None:
            col["width"] = widths[i]
        table.add_column(**col)

    return table


def _render_and_cache_widths(
    pkgs: list[Package], term_width: int
) -> tuple[Table, tuple[int, ...]]:
    """Build, populate and render a table and resolve column widths.

    Args:
        pkgs: The packages to populate the table with.
        term_width: The terminal width to render against.

    Returns:
        A rendered table using the resolved column widths.
    """
    measuring = _MeasuringTable(box=box.MINIMAL_HEAVY_HEAD)
    cols: list[dict] = COLUMN_DEFINITIONS

    for col in cols:
        measuring.add_column(**col)
    _populate_rows(measuring, pkgs)

    scratch = Console(record=True, width=term_width)
    with scratch.capture():
        scratch.print(measuring)

    widths = measuring.resolved_widths or ()
    valid = widths and not any(w == 0 for w in widths)

    display_table = _build_table(widths=widths if valid else None)
    _populate_rows(display_table, pkgs)

    return display_table, widths


def status_to_str(status: PackageStatus) -> str:
    """Convert PackageStatus to a human-readable string with color coding.

    Args:
        status: The PackageStatus to convert.

    Returns:
        A human-readable string representation of the PackageStatus.
    """
    if status == PackageStatus.NONE:
        return "[green]Up-to-date[/green]"
    bits: list[str] = [label for flag, label in STATUS_LABELS.items() if flag in status]

    return ", ".join(bits)


def _save_width_cache() -> None:
    """Save calculated column widths to file cache"""
    try:
        ensure_cache_dir()
        WIDTHS_CACHE.write_text(
            data=json.dumps(
                obj={str(object=k): list(v) for k, v in _width_cache.items()}
            )
        )

    except Exception:
        pass


def package_table(pkgs: Iterable[Package]) -> Table:
    """Create a Rich Table displaying package information.

    Uses cached column width measurements, except on first call or terminal resizing.

    Args:
        pkgs: An iterable of Package instances to display.

    Returns:
        A Rich Table displaying package information.
    """
    _ensure_width_cache_loaded()

    pkg_list: list[Package] = list(pkgs)
    term_width, _ = _terminal_size()
    cached_widths: tuple[int, ...] | None = _width_cache.get(term_width)

    if cached_widths:
        table: Table = _build_table(widths=cached_widths)
        _populate_rows(table=table, pkgs=pkg_list)
        return table

    table, widths = _render_and_cache_widths(pkgs=pkg_list, term_width=term_width)
    if widths and not any(w == 0 for w in widths):
        _width_cache[term_width] = widths
        _save_width_cache()

    return table


def _populate_rows(table: Table, pkgs: list[Package]) -> None:
    """Add all package rows to the table."""
    for p in pkgs:
        installed: str = p.versions[0] if p.versions else ""
        latest = p.metadata.get("latest_version") or (
            p.versions[-1] if p.versions else ""
        )
        size_mb: str = f"{(p.size_kb or 0) / 1024:.2f}" if p.size_kb else ""
        table.add_row(
            p.kind.value,
            p.name,
            installed,
            latest,
            status_to_str(p.status),
            size_mb,
            p.installed_on.isoformat() if p.installed_on else "",
        )


def paginate(pkgs: list[Package], page_size: int, console: Console) -> None:
    """Paginate the table of packages.

    Args:
        pkgs: List of packages to paginate
        page_size: Number of packages to display per page
        console: Console instance to display output
    """
    page = 0
    total_pages = -(-len(pkgs) // page_size)

    with console.screen():
        while True:
            start = page * page_size
            console.print(package_table(pkgs[start : start + page_size]))
            console.print(
                f"\n[dim]Page {page + 1}/{total_pages} · "
                f"[bold]n[/bold] next  [bold]p[/bold] prev  [bold]q[/bold] quit[/dim]"
            )

            key = readchar.readkey()
            if (
                key in ("n", readchar.key.RIGHT, readchar.key.SPACE)
                and page < total_pages - 1
            ):
                page += 1
            elif key in ("p", readchar.key.LEFT) and page > 0:
                page -= 1
            elif key in ("q", readchar.key.ENTER, readchar.key.ESC):
                break


def package_details(pkg: Package) -> Table:
    """Display detailed information about a package.

    Args:
        pkg: The package to display information for.

    Returns:
        A Rich Table displaying detailed information about the package.
    """
    t = Table(box=box.MINIMAL, show_header=False)
    t.add_row("Name", pkg.name, style="bold blue")
    t.add_row("Kind", pkg.kind.value)
    t.add_row("Description", pkg.desc or "")

    latest = pkg.metadata.get("latest_version") or ""
    installed_display = [v for v in pkg.versions if v != latest] or pkg.versions

    t.add_row("Installed Versions", ", ".join(installed_display))
    t.add_row("Latest", latest)
    t.add_row("Status", status_to_str(pkg.status))
    t.add_row("Size (MB)", f"{(pkg.size_kb or 0) / 1024:.2f}")

    if pkg.deps:
        t.add_row("Depends on", ", ".join(d.name for d in pkg.deps))
    if pkg.used_by:
        t.add_row("Used by", ", ".join(pkg.used_by), style="dim")
    if pkg.tap:
        t.add_row("Tap", pkg.tap)
    if pkg.path:
        t.add_row("Path", str(object=pkg.path))

    return t
