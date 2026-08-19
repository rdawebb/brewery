"""Install formulae and casks, verifying what actually landed in the prefix."""

from __future__ import annotations

from typing import TYPE_CHECKING

from brewery.core.config import BreweryENV
from brewery.core.decorators import log_operation
from brewery.core.models import Notes, Package, PackageKind
from brewery.core.repo import Repository
from brewery.core.shell import run_brew
from brewery.providers import brew
from brewery.providers.base import PackageBackend
from brewery.providers.pipeline import run_install
from brewery.services.cask import install_casks

if TYPE_CHECKING:
    from brewery.providers.orchestrator import ProgressPort


@log_operation(event_prefix="install_package", log_args=["name", "kind"])
async def install_packages(
    repo: Repository,
    names: list[str],
    kind: PackageKind = PackageKind.FORMULA,
    *,
    env: BreweryENV | None = None,
    formula: PackageBackend = brew.formula_backend,
    cask: PackageBackend = brew.cask_backend,
    progress: ProgressPort | None = None,
) -> tuple[list[Package], Notes]:
    """Install packages and report what the re-scan found.

    Success is decided by the filesystem, not by the backend's exit status: a
    name that is absent after the re-scan is a failure even if the install
    reported none.

    Args:
        repo: The data facade to read installed state and aliases through.
        names: Name(s) of the package(s) to install.
        kind: Kind of the package(s) - formula (default) or cask.
        env: Brewery environment (paths), resolved by the pipeline if omitted.
        formula: Formula backend for the per-formula brew fallback.
        cask: Cask backend, which handles casks wholesale.
        progress: Optional progress sink for the native pipeline.

    Returns:
        Tuple of (installed packages, (name, reason) failures).

    Raises:
        BrewCommandError: Propagated from provider.
    """
    if kind == PackageKind.CASK:
        await install_casks(names, backend=cask)

    else:
        await run_install(
            names,
            catalog=repo.catalog,
            cache_mgr=repo.cache_mgr,
            formula=formula,
            run_brew=run_brew,
            env=env,
            progress=progress,
        )

    repo.cache_mgr.invalidate()
    installed_by_name: dict[str, Package] = {
        p.name: p for p in repo.cache_mgr.installed_packages(kind=kind)
    }

    resolved: dict[str, str] = {n: repo.catalog.resolve_alias(n) for n in names}

    installed: list[Package] = [
        installed_by_name[resolved[n]]
        for n in names
        if resolved[n] in installed_by_name
    ]

    failures: Notes = [
        (n, "install failed or not found")
        for n in names
        if resolved[n] not in installed_by_name
    ]

    return installed, failures
