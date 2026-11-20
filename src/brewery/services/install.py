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


@log_operation(event_prefix="install_package", log_args=["names", "kind"])
async def install_packages(
    repo: Repository,
    names: list[str],
    kind: PackageKind | None = None,
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
        kind: Kind of the package(s); None picks per name: a formula when the catalog
            has one, else a cask, else passed to brew as a formula.
        env: Brewery environment (paths), resolved by the pipeline if omitted.
        formula: Formula backend for the per-formula brew fallback.
        cask: Cask backend, for the casks brewery can't install natively.
        progress: Optional progress sink for the native pipeline.

    Returns:
        Tuple of (installed packages, (name, reason) failures).

    Raises:
        BrewCommandError: Propagated from provider.
    """
    casks = names if kind == PackageKind.CASK else []
    if kind is None:
        casks = [
            n
            for n in names
            if repo.catalog.get_formula(repo.catalog.resolve_alias(n)) is None
            and repo.catalog.get_cask(n) is not None
        ]

    formulae = [n for n in names if n not in casks]
    if casks:
        # Any formulae run alongside, in the casks' display
        notes = dict(
            await install_casks(
                casks,
                repo=repo,
                backend=cask,
                formula=formula,
                env=env,
                progress=progress,
                formulae=formulae,
            )
        )

    else:
        report = await run_install(
            names,
            catalog=repo.catalog,
            cache_mgr=repo.cache_mgr,
            formula=formula,
            run_brew=run_brew,
            env=env,
            progress=progress,
        )
        notes = report.errors

    repo.cache_mgr.invalidate()
    by_key: dict[tuple[str, PackageKind], Package] = {
        (p.name, p.kind): p for p in repo.cache_mgr.installed_packages()
    }

    keys: dict[str, tuple[str, PackageKind]] = {
        n: (repo.catalog.resolve_alias(n), PackageKind.FORMULA) for n in formulae
    } | {n: (n, PackageKind.CASK) for n in casks}

    installed: list[Package] = [by_key[keys[n]] for n in names if keys[n] in by_key]
    failures: Notes = [
        (n, notes.get(keys[n][0], notes.get(n, "install failed or not found")))
        for n in names
        if keys[n] not in by_key
    ]

    return installed, failures
