"""Uninstall command: uninstall a package or list of packages."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console, run_async
from brewery.cli.error_formatting import handle_error
from brewery.core.models import PackageKind


@app.command(aliases=["rm", "del"])
def uninstall(
    names: list[str],
    kind: PackageKind | None = app.Option(
        None, "--kind", help="formula | cask | auto (default)"
    ),
    yes: bool = app.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Uninstall a package or list of packages.

    Args:
        names: Name(s) of the package(s) to uninstall.
        kind: Kind of the package(s) (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    pkg_str: str = ", ".join(names)

    try:
        if not yes:
            if not app.confirm(text=f"Uninstall: {pkg_str}?", default=False):
                console.print("\nUninstallation cancelled\n", style="dim")
                return

        with _repository() as repo:
            app.echo()
            with console.status(
                status="[bold yellow]Uninstalling...[/bold yellow]",
                refresh_per_second=5,
            ):
                removed, failures = run_async(coro=repo.uninstall_packages(names, kind))

            console.print(
                f"✓ Uninstalled {len(removed)} package(s)\n", style="bold green"
            )
            for pkg in removed:
                console.print(f"  [dim]-[/dim] {pkg}")

            if failures:
                console.print(
                    f"✗ Failed to uninstall {len(failures)} package(s)",
                    style="bold red",
                )
                for name, reason in failures:
                    console.print(f"  [dim]-[/dim] {name} - {reason}")

            app.echo()

    except KeyboardInterrupt:
        console.print(
            "\n⚠ Interrupted. Re-run [bold]brewery uninstall <name>[/bold] to complete it\n",
            style="bold yellow",
        )
        sys.exit(130)

    except Exception as e:
        sys.exit(handle_error(error=e))
