"""Renderers for displaying package information in the CLI using Rich."""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

import orjson
import readchar
from rich import box
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.table import Table
from rich.text import Text

from brewery.core.config import ensure_cache_dir
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import Package, PackageKind, PackageStatus

log: BreweryLogger = get_logger(name=__name__)

# flag -> (colour, label)
_STATUS_STYLES: dict[PackageStatus, tuple[str, str]] = {
    PackageStatus.PINNED: ("yellow", "Pinned"),
    PackageStatus.OUTDATED: ("red", "Outdated"),
    PackageStatus.NOT_LINKED: ("blue", "Not Linked"),
    PackageStatus.KEG_ONLY: ("magenta", "Keg-Only"),
    PackageStatus.HEAD: ("cyan", "HEAD"),
    PackageStatus.HAS_SERVICE: ("green", "Service"),
}

STATUS_LABELS: dict[PackageStatus, str] = {
    flag: f"[{colour}]{label}[/{colour}]"
    for flag, (colour, label) in _STATUS_STYLES.items()
}

# The states worth colouring in the compact view (and a count in the section header)
_COLOURED_FLAGS: tuple[PackageStatus, ...] = (
    PackageStatus.PINNED,
    PackageStatus.OUTDATED,
    PackageStatus.NOT_LINKED,
)

# Fixed left-to-right order for the section-header summary
_SUMMARY_ORDER: tuple[PackageStatus, ...] = (
    PackageStatus.OUTDATED,
    PackageStatus.PINNED,
    PackageStatus.NOT_LINKED,
)

COLUMN_DEFINITIONS: list[dict] = [
    {"header": "Kind"},
    {"header": "Name", "style": "bold"},
    {"header": "Installed"},
    {"header": "Latest"},
    {"header": "Status"},
    {"header": "Size (MB)", "justify": "right"},
    {"header": "Installed On", "style": "dim"},
]

# Local wall-clock, without the offset an aware isoformat would append
_INSTALLED_ON_FORMAT = "%Y-%m-%d %H:%M"

_WIDTHS_FILENAME = "column_widths.json"


def _widths_cache_path() -> Path:
    """Path of the on-disk column-width cache, creating the cache dir if needed.

    Resolved lazily rather than at import time, so importing the CLI never touches
    the filesystem.

    Returns:
        The column-width cache file path.
    """
    return ensure_cache_dir() / _WIDTHS_FILENAME


# Terminal width mapped to column headers
_width_cache: dict[int, tuple[int, ...]] = {}
_width_cache_loaded = False


def _load_width_cache() -> None:
    """Load pre-computed column widths from cache."""
    try:
        path: Path = _widths_cache_path()
        if path.exists():
            data: dict[int, tuple[int, ...]] = orjson.loads(path.read_bytes())
            _width_cache.update({int(k): tuple(v) for k, v in data.items()})

    # An unreadable or malformed cache just means widths get recomputed
    except (OSError, ValueError, TypeError, AttributeError) as e:
        log.debug(event="width_cache_read_failed", error=str(object=e))


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


def _status_colour(pkg: Package) -> str | None:
    """Return the compact-view status colour for a package's name, or None.

    Picks the first flag present on the package in `_STATUS_STYLES` order (which is
    pinned-first) that also earns a colour.

    Args:
        pkg: The package to colour.

    Returns:
        A colour name, or None when the name should keep its default style.
    """
    for flag, (colour, _label) in _STATUS_STYLES.items():
        if flag in _COLOURED_FLAGS and flag in pkg.status:
            return colour

    return None


def _compact_entry(pkg: Package, *, mark_installed: bool) -> Text:
    """Build a single compact-view cell: name (status-coloured) and optional install tick.

    Args:
        pkg: The package to render.
        mark_installed: When true, style green and append a tick for installed packages.
            Used by `search`, where results are mostly not installed; `path is not None`
            is the installed discriminator.

    Returns:
        A Rich `Text` for the package.
    """
    colour: str | None = _status_colour(pkg)
    entry = Text(pkg.name, style=colour or "")

    if mark_installed and pkg.path is not None:
        entry.append(" ✓").style = "green"

    return entry


def _section_summary(pkgs: list[Package]) -> Text:
    """Summarise a section's bulleted states as 'N outdated / M pinned', zeros omitted.

    Each count is coloured to match its name colour; the separators stay dim.

    Args:
        pkgs: The packages in one section (all the same kind).

    Returns:
        A summary Text, empty when no package carries a coloured state.
    """
    summary = Text()

    for flag in _SUMMARY_ORDER:
        count: int = sum(1 for p in pkgs if flag in p.status)
        if count:
            colour, label = _STATUS_STYLES[flag]
            if summary:
                summary.append(" / ", style="dim")
            summary.append(f"{count} {label.lower()}", style=colour)

    return summary


def _section(
    pkgs: list[Package], header: str, *, mark_installed: bool, single_column: bool
) -> Group:
    """Build one titled section (header + entries) for the compact view.

    Args:
        pkgs: The packages in this section, already filtered to one kind.
        header: The section title, e.g. "Formulae".
        mark_installed: Passed through to `_compact_entry`.
        single_column: When true, render one entry per line instead of a column grid
            (used for non-tty output).

    Returns:
        A renderable group for the section.
    """
    ordered: list[Package] = sorted(pkgs, key=lambda p: p.name.lower())
    entries: list[Text] = [
        _compact_entry(p, mark_installed=mark_installed) for p in ordered
    ]

    title = Text(f"==> {header}", style="bold blue")
    summary: Text = _section_summary(ordered)
    if summary:
        title.append(" - ", style="dim")
        title.append_text(summary)

    body: RenderableType = (
        Group(*entries)
        if single_column
        else Columns(entries, padding=(0, 6), equal=True, column_first=True)
    )

    return Group(title, body)


def package_columns(
    pkgs: Iterable[Package],
    *,
    mark_installed: bool = False,
    single_column: bool = False,
) -> RenderableType:
    """Render packages as a compact, brew-style multi-column view.

    Packages are split into Formulae and Casks sections, each sorted by name, with a
    header summary of coloured states and the name itself coloured on any package that
    is pinned, outdated or not-linked.

    Args:
        pkgs: The packages to render.
        mark_installed: Mark installed packages with a green tick (for `search`).
        single_column: Render one entry per line rather than a grid (non-tty output).

    Returns:
        A Rich renderable for the whole view.
    """
    pkg_list: list[Package] = list(pkgs)
    formulae: list[Package] = [p for p in pkg_list if p.kind is PackageKind.FORMULA]
    casks: list[Package] = [p for p in pkg_list if p.kind is PackageKind.CASK]

    sections: list[Group] = []
    for section_pkgs, header in ((formulae, "Formulae"), (casks, "Casks")):
        if section_pkgs:
            sections.append(
                _section(
                    section_pkgs,
                    header,
                    mark_installed=mark_installed,
                    single_column=single_column,
                )
            )

    if not sections:
        return Text("\n- No packages found\n", style="dim")

    blocks: list[RenderableType] = [""]  # Blank line before the first title
    for i, section in enumerate(sections):
        if i:
            blocks.append("")  # Blank line between sections
        blocks.append(section)
    blocks.append("")  # Blank line after the last section

    return Group(*blocks)


def _save_width_cache() -> None:
    """Save calculated column widths to file cache"""
    try:
        _widths_cache_path().write_bytes(
            orjson.dumps({str(k): list(v) for k, v in _width_cache.items()})
        )

    # The cache is an optimisation; failing to persist it is not an error
    except (OSError, TypeError) as e:
        log.debug(event="width_cache_write_failed", error=str(object=e))


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
    """Add all package rows to the table.

    Args:
        table: The table to populate.
        pkgs: The list of packages to add to the table.
    """
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
            p.installed_on.strftime(_INSTALLED_ON_FORMAT) if p.installed_on else "",
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

    with console.screen() as screen:
        while True:
            start = page * page_size
            table = package_table(pkgs[start : start + page_size])
            nav_text = Text.from_markup(
                f"\n[dim]Page {page + 1}/{total_pages} · "
                f"[bold]n[/bold] next  [bold]p[/bold] prev  [bold]q[/bold] quit[/dim]",
                justify="center",
            )

            layout = Layout()
            layout.split_column(
                Layout(table, name="table"),
                Layout(nav_text, name="nav", size=2),
            )
            screen.update(layout)

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


def _version_cell(pkg: Package, installed: bool) -> str:
    """Build the Version cell for the package details table.

    Args:
        pkg: The package to display the version for.
        installed: Whether the package is installed.

    Returns:
        A Rich-markup string for the Version cell.
    """
    latest: str = pkg.metadata.get("latest_version") or ""

    if not installed:
        version: str = pkg.versions[0] if pkg.versions else latest
        return f"{version} [bold red]✗[/bold red]"

    version = pkg.versions[0] if pkg.versions else ""
    if PackageStatus.OUTDATED in pkg.status:
        return f"{version} [bold yellow]↑[/bold yellow] {latest}"

    return f"{version} [bold green]✓[/bold green]"


def _status_cell(pkg: Package) -> str:
    """Build the Status cell from keg flags.

    Args:
        pkg: The package to display the status for.

    Returns:
        A Rich-markup string for the Status cell, or "" when nothing to show.
    """
    bits: list[str] = [
        label
        for flag, label in STATUS_LABELS.items()
        if flag is not PackageStatus.OUTDATED and flag in pkg.status
    ]

    return ", ".join(bits)


def package_details(pkg: Package) -> Table:
    """Display detailed information about a package.

    Args:
        pkg: The package to display information for.

    Returns:
        A Rich Table displaying detailed information about the package.
    """
    installed: bool = pkg.path is not None

    t = Table(box=box.MINIMAL, show_header=False)
    t.add_row("Name", pkg.name, style="bold blue")
    t.add_row("Kind", pkg.kind.value)
    t.add_row("Description", pkg.desc or "")
    t.add_row("Version", _version_cell(pkg, installed))

    if installed:
        t.add_row("Size (MB)", f"{(pkg.size_kb or 0) / 1024:.2f}")

    status: str = _status_cell(pkg)
    if status:
        t.add_row("Status", status)

    if pkg.deps:
        t.add_row("Depends on", ", ".join(d.name for d in pkg.deps), style="dim")
    if pkg.used_by:
        t.add_row("Used by", ", ".join(pkg.used_by), style="dim")
    if pkg.tap:
        t.add_row("Tap", pkg.tap)
    if pkg.path:
        t.add_row("Path", str(object=pkg.path))

    return t
