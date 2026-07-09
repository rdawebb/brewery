"""Install command: install a package or list of packages."""

from __future__ import annotations

import sys

from brewery.cli.context import _repository, app, console, run_async
from brewery.cli.error_formatting import handle_error
from brewery.core.errors import AlreadyInstalledWarning
from brewery.core.models import PackageKind


@app.command(aliases=["add"])
def install(
    names: list[str] = app.Argument(...),
    kind: PackageKind | None = app.Option(
        None, "--kind", help="formula | cask (default: formula)"
    ),
    yes: bool = app.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Install a package or list of packages.

    Args:
        names: Name(s) of the package(s) to install.
        kind: Kind of the package(s) (formula or cask).
        yes: If true, skip confirmation prompt.
    """
    try:
        kind: PackageKind = kind or PackageKind.FORMULA
        if not yes:
            pkg_str: str = ", ".join(names)
            if not app.confirm(text=f"Install {kind.value}: {pkg_str}?", default=True):
                console.print("\nInstallation cancelled\n", style="dim")
                return

        with _repository() as repo:
            app.echo()
            with console.status(
                status="[bold green]Installing...[/bold green]", refresh_per_second=5
            ):
                installed, failures = run_async(coro=repo.install_packages(names, kind))

            console.print(
                f"✓ Installed {len(installed)} package(s)\n", style="bold green"
            )
            for pkg in installed:
                console.print(
                    f"  [dim]→[/dim] {pkg.name} {pkg.versions[0] if pkg.versions else ''}"
                )

            if failures:
                console.print(
                    f"✗ Failed to install {len(failures)} package(s)", style="bold red"
                )
                for name, reason in failures:
                    console.print(f"  [dim]-[/dim] {name} - {reason}")

            app.echo()

    except AlreadyInstalledWarning as e:
        console.print(f"\n⚠ {e.message}\n", style="bold yellow")

    except KeyboardInterrupt:
        console.print(
            "\n⚠ Interrupted. Re-run [bold]brewery install <name>[/bold] to complete it\n",
            style="bold yellow",
        )
        sys.exit(130)

    except Exception as e:
        sys.exit(handle_error(error=e))
