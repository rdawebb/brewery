"""Renderers for displaying package information in the CLI using Rich."""

import time
from typing import Iterable

from rich import box
from rich.table import Table

from brewery.core.models import Package, PackageStatus


STATUS_LABELS = {
    PackageStatus.OUTDATED: "[red]Outdated[/red]",
    PackageStatus.PINNED: "[yellow]Pinned[/yellow]",
    PackageStatus.NOT_LINKED: "[blue]Not Linked[/blue]",
    PackageStatus.KEG_ONLY: "[magenta]Keg-Only[/magenta]",
    PackageStatus.HEAD: "[cyan]HEAD[/cyan]",
    PackageStatus.HAS_SERVICE: "[green]Service[/green]",
}


def status_to_str(status: PackageStatus) -> str:
    """Convert PackageStatus to a human-readable string with color coding.

    Args:
        status: The PackageStatus to convert.

    Returns:
        A human-readable string representation of the PackageStatus.
    """
    if status == PackageStatus.NONE:
        return "[green]Up-to-date[/green]"
    bits = [label for flag, label in STATUS_LABELS.items() if flag in status]
    return ", ".join(bits)


def package_table(pkgs: Iterable[Package]) -> Table:
    """Create a Rich Table displaying package information.

    Args:
        pkgs: An iterable of Package instances to display.

    Returns:
        A Rich Table displaying package information.
    """
    start = time.perf_counter()
    table = Table(box=box.MINIMAL_HEAVY_HEAD)
    table.add_column("Kind", style="bold")
    table.add_column("Name", style="bold")
    table.add_column("Installed")
    table.add_column("Latest")
    table.add_column("Status")
    table.add_column("Size (MB)", justify="right")
    table.add_column("Installed On", style="dim")
    print(f"Table setup time: {(time.perf_counter() - start) * 1000:.2f} ms")

    for p in pkgs:
        installed = p.versions[0] if p.versions else ""
        latest = p.metadata.get("latest_version") or (
            p.versions[-1] if p.versions else ""
        )
        size_mb = f"{(p.size_kb or 0) // (1024):.2f}" if p.size_kb else ""
        table.add_row(
            p.kind.value,
            p.name,
            installed,
            latest,
            status_to_str(p.status),
            size_mb,
            p.installed_on.isoformat() if p.installed_on else "",
        )
    print(f"After adding rows: {(time.perf_counter() - start) * 1000:.2f} ms")

    return table


def package_details(pkg: Package) -> Table:
    """Display detailed information about a package.

    Args:
        pkg: The package to display information for.

    Returns:
        A Rich Table displaying detailed information about the package.
    """
    t = Table(box=box.MINIMAL_HEAVY_HEAD)
    t.add_column("Field", style="bold")
    t.add_column("Value")
    t.add_row("Name", pkg.name)
    t.add_row("Kind", pkg.kind.value)
    t.add_row("Description", pkg.desc or "")
    t.add_row("Installed Versions", ", ".join(pkg.versions))
    t.add_row("Latest", pkg.metadata.get("latest_version") or "")
    t.add_row("Status", status_to_str(pkg.status))
    t.add_row("Size (MB)", f"{(pkg.size_kb or 0) // 1024:.2f}")
    if pkg.deps:
        t.add_row("Depends on", ", ".join(d.name for d in pkg.deps))
    if pkg.used_by:
        t.add_row("Used by", ", ".join(pkg.used_by))
    if pkg.tap:
        t.add_row("Tap", pkg.tap)
    if pkg.path:
        t.add_row("Path", str(pkg.path))

    return t
