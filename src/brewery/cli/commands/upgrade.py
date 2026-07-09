"""Upgrade command: upgrade one, list, or all outdated packages."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console, run_async
from brewery.cli.error_formatting import handle_error
from brewery.core.errors import PinnedPackageWarning
from brewery.core.models import Package, PackageKind


@app.command(aliases=["u", "up"])
def upgrade(
    names: list[str] | None = app.Argument(
        None, help="Package(s) to upgrade (leave empty to upgrade all)"
    ),
    kind: PackageKind | None = app.Option(
        None, "--kind", help="formula | cask | auto (default)"
    ),
    yes: bool = app.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Upgrade one, list, or all outdated packages.

    Args:
        names: Name(s) of the package(s) to upgrade (if None, upgrades all outdated).
        kind: Kind of the package (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    try:
        with _repository() as repo:
            if not yes:
                if names:
                    pkg_str: str = ", ".join(names)
                    if not app.confirm(text=f"Upgrade: {pkg_str}?", default=True):
                        console.print("Upgrade cancelled.", style="dim")
                        return

                else:
                    outdated: list[Package] = repo.get_outdated()
                    if not outdated:
                        console.print(
                            "\n✓ All packages are up to date!\n", style="bold green"
                        )
                        return

                    console.print(
                        f"\n• {len(outdated)} outdated package(s)\n",
                        style="bold yellow",
                    )
                    for pkg in outdated:
                        latest = pkg.metadata.get("latest_version")
                        console.print(f"  [dim]-[/dim] {pkg.name} → {latest}")

                    app.echo()
                    if not app.confirm(
                        text=f"Upgrade {len(outdated)} outdated package(s)?",
                        default=True,
                    ):
                        console.print("Upgrade cancelled.", style="dim")
                        return

            app.echo()
            with console.status(
                status="[bold yellow]Upgrading...[/bold yellow]", refresh_per_second=5
            ):
                upgraded, current, failures = run_async(
                    coro=repo.upgrade_packages(names, kind)
                )

            if not upgraded and not failures and not current:
                console.print("✓ All packages are up to date!\n", style="bold green")
                return

            console.print(
                f"✓ Upgraded {len(upgraded)} package(s)\n", style="bold green"
            )
            for pkg in upgraded:
                console.print(
                    f"  [dim]→[/dim] {pkg.name} {pkg.versions[0] if pkg.versions else ''}"
                )

            if current:
                console.print(f"\n{len(current)} already up-to-date:\n", style="dim")
                for pkg in current:
                    console.print(
                        f"  - {pkg.name} {pkg.versions[0] if pkg.versions else ''}",
                        style="dim",
                    )

            if failures:
                console.print(f"\n✗ {len(failures)} skipped/failed:", style="bold red")
                for pkg_name, reason in failures:
                    console.print(f"  - {pkg_name}: [dim]{reason}[/dim]")

            app.echo()

    except PinnedPackageWarning as e:
        console.print(f"\n[bold yellow]⚠ {e.message}[/bold yellow]\n")

    except KeyboardInterrupt:
        console.print(
            "\n⚠ Interrupted. Re-run [bold]brewery upgrade <name>[/bold] to complete it\n",
            style="bold yellow",
        )
        sys.exit(130)

    except Exception as e:
        sys.exit(handle_error(error=e))
