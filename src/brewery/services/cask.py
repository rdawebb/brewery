"""Cask operations: handled natively where possible, otherwise by brew."""

from __future__ import annotations

import asyncio
import functools
import os
from typing import TYPE_CHECKING

import httpx2

from brewery.core.cask_locale import config_languages, localise, system_languages
from brewery.core.catalog import CaskRow
from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.errors import BrewError
from brewery.core.host import current_platform
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import PackageKind
from brewery.core.shell import run_brew
from brewery.providers.base import (
    InstallBackend,
    PackageBackend,
    UninstallBackend,
    UpgradeBackend,
)
from brewery.providers.cask import (
    CaskOpts,
    classify,
    classify_uninstall,
    read_cask_opts,
)
from brewery.providers.caskroom import (
    install_artifacts,
    read_receipt,
    stage,
    uninstall_cask,
    unstage,
    upgrade_cask,
)
from brewery.providers.downloader import NO_CHECK, BottleRef, Downloader
from brewery.providers.orchestrator import _NULL_PROGRESS, Outcome, ProgressPort
from brewery.providers.pipeline import PIPELINE_TIMEOUT, run_install

if TYPE_CHECKING:
    from pathlib import Path

    from brewery.core.repo import Repository

log: BreweryLogger = get_logger(name=__name__)


async def install_casks(
    names: list[str],
    *,
    repo: Repository,
    backend: InstallBackend,
    formula: PackageBackend,
    env: BreweryENV | None = None,
    progress: ProgressPort | None = None,
) -> None:
    """Install every named cask, natively where possible, the rest through brew.

    Casks already installed are skipped. A cask's `depends_on cask` is installed
    natively first, and its `depends_on formula` through the formula pipeline; a
    cask whose native install or dependencies fail goes to brew, which then installs
    those dependencies.

    Args:
        names: Cask tokens to install.
        repo: The data facade to read the catalog and installed state through.
        backend: The cask backend for the casks brewery can't install.
        formula: Formula backend for the formula pipeline's brew fallback.
        env: Brewery environment (paths), resolved if omitted.
        progress: Optional progress sink for the native installs.
    """
    env = env or get_brewery_env()
    progress = progress or _NULL_PROGRESS
    opts = read_cask_opts(os.environ.get("HOMEBREW_CASK_OPTS", ""))
    installed = {p.name for p in repo.cache_mgr.installed_packages(PackageKind.CASK)}
    requested = [n for n in dict.fromkeys(names) if n not in installed]

    rows: dict[str, CaskRow] = {}
    order: list[str] = []  # Dependencies before their dependents
    languages: list[str] = []  # Read from the system only for a cask that has variants

    def visit(token: str) -> None:
        """Visit a cask and its dependencies, recording the order to install them in."""
        nonlocal languages
        if token in rows or token in installed:
            return

        if (row := repo.catalog.get_cask(token)) is None:
            return  # brew reports the unknown name

        if row.language_variations:
            languages = languages or [*opts.languages, *system_languages()]
            row = localise(row, languages)

        rows[token] = row
        for dep in _depends(row, "cask"):
            visit(dep)
        order.append(token)

    for token in requested:
        visit(token)

    reasons = {t: _brew_reason(rows[t], opts) for t in order}
    native = [t for t in order if not reasons[t]]
    failed_formulae = await _install_formulae(
        sorted({f for t in native for f in _depends(rows[t], "formula")}),
        repo=repo,
        formula=formula,
        env=env,
        progress=progress,
    )

    done = set(installed)
    if native:
        progress.begin(len(native))
        try:
            async with httpx2.AsyncClient(timeout=PIPELINE_TIMEOUT) as client:
                downloader = Downloader(cache_dir=env.bottle_cache, client=client)

                for token in native:
                    row = rows[token]
                    reasons[token] = _blocked(row, done, failed_formulae) or (
                        await _install_one(
                            row,
                            downloader,
                            env,
                            opts,
                            progress,
                            on_request=token in requested,
                        )
                    )

                    if not reasons[token]:
                        done.add(token)
                        progress.finish(token, Outcome.NATIVE.value)

                    else:
                        progress.update(token, "brew")
                        progress.finish(token, Outcome.BREW_INSTALL.value)

        finally:
            progress.end()

    for token, reason in reasons.items():
        if reason:
            log.info(event="cask_to_brew", cask=token, reason=reason)

    # brew installs a dependency of a cask it's given, so only requested casks go
    to_brew = [t for t in requested if t not in done]
    if to_brew:
        # After the live display ends: a pkg cask may prompt for a sudo password
        await backend.install(names=to_brew)


def _depends(row: CaskRow, kind: str) -> list[str]:
    """The cask's `depends_on` names of one kind.

    Args:
        row: The cask's catalog row.
        kind: `"cask"` or `"formula"`.

    Returns:
        The names, possibly empty.
    """
    return list((row.depends_on or {}).get(kind) or [])


def _brew_reason(row: CaskRow, opts: CaskOpts) -> str:
    """Why a cask can't be installed natively, before trying.

    Args:
        row: The cask's catalog row.
        opts: The parsed `HOMEBREW_CASK_OPTS`.

    Returns:
        The reason, or an empty string for a native cask.
    """
    if not row.url or not row.version:
        return "no download"

    if opts.require_sha and row.sha256 in (None, NO_CHECK):
        return "--require-sha"

    _route, reason = classify(row, current_platform())

    return reason


def _blocked(row: CaskRow, done: set[str], failed_formulae: set[str]) -> str:
    """Why a native cask's dependencies rule it out.

    Args:
        row: The cask's catalog row.
        done: Casks installed so far, natively or before this run.
        failed_formulae: `depends_on formula` names that didn't install.

    Returns:
        The reason, or an empty string if every dependency is in place.
    """
    for dep in _depends(row, "cask"):
        if dep not in done:
            return f"depends on cask {dep}"

    for dep in _depends(row, "formula"):
        if dep in failed_formulae:
            return f"depends on formula {dep}"

    return ""


async def _install_formulae(
    names: list[str],
    *,
    repo: Repository,
    formula: PackageBackend,
    env: BreweryENV,
    progress: ProgressPort,
) -> set[str]:
    """Install casks' formula dependencies through the formula pipeline.

    Args:
        names: The formula names.
        repo: The data facade.
        formula: Formula backend for the pipeline's brew fallback.
        env: Brewery environment.
        progress: The progress sink.

    Returns:
        The names that didn't install.
    """
    if not names:
        return set()

    try:
        report = await run_install(
            names,
            catalog=repo.catalog,
            cache_mgr=repo.cache_mgr,
            formula=formula,
            run_brew=run_brew,
            env=env,
            progress=progress,
        )

    except BrewError as exc:
        log.warning(event="cask_formula_deps_failed", error=str(exc))
        return set(names)

    repo.cache_mgr.invalidate()
    return set(report.failed)


async def _install_one(
    row: CaskRow,
    downloader: Downloader,
    env: BreweryENV,
    opts: CaskOpts,
    progress: ProgressPort,
    *,
    on_request: bool,
) -> str:
    """Download, stage and install one cask, unstaging it if any step fails.

    Args:
        row: The cask's catalog row.
        downloader: The open downloader.
        env: Brewery environment.
        opts: The parsed `HOMEBREW_CASK_OPTS`.
        progress: The progress sink.
        on_request: Whether the user asked for it, rather than a dependent cask.

    Returns:
        The reason it failed, or an empty string once it's installed.
    """
    download = await _fetch(row, downloader, progress)
    if isinstance(download, str):
        return download

    try:
        progress.update(row.token, "install")
        await asyncio.to_thread(
            _stage_and_install, row, download, env, opts, on_request
        )

    except (BrewError, OSError) as exc:
        return str(exc)

    return ""


def _stage_and_install(
    row: CaskRow, download: Path, env: BreweryENV, opts: CaskOpts, on_request: bool
) -> None:
    """Stage a download and install its artifacts, or leave no trace of it.

    Args:
        row: The cask's catalog row.
        download: The verified download.
        env: Brewery environment.
        opts: The parsed `HOMEBREW_CASK_OPTS`.
        on_request: Whether the user asked for it.
    """
    staged = stage(row, download, env.caskroom, on_request=on_request)
    try:
        install_artifacts(
            row,
            staged,
            env,
            dirs=opts.dirs,
            binaries=opts.binaries,
            languages=opts.languages,
        )

    except BaseException:
        unstage(row.token, str(row.version), env.caskroom)
        raise


async def uninstall_casks(
    names: list[str],
    *,
    backend: UninstallBackend,
    env: BreweryENV | None = None,
    zap: bool = False,
) -> None:
    """Uninstall every named cask, natively where possible, the rest through brew.

    A cask whose native uninstall fails part way goes to brew, which retries it.

    Args:
        names: Cask tokens to uninstall.
        backend: The cask backend for the casks brewery can't uninstall.
        env: Brewery environment (paths), resolved if omitted.
        zap: Also remove each cask's `zap` files, as `--zap` does.
    """
    env = env or get_brewery_env()
    plat = current_platform()
    to_brew: list[str] = []
    for token in names:
        reason = classify_uninstall(read_receipt(env.caskroom / token), plat, zap=zap)
        if not reason:
            try:
                await asyncio.to_thread(uninstall_cask, token, env, zap=zap)
                continue

            except (BrewError, OSError) as exc:
                reason = str(exc)

        log.info(event="cask_to_brew", cask=token, reason=reason)
        to_brew.append(token)

    if to_brew:
        await backend.uninstall(names=to_brew, zap=zap)


async def _fetch(
    row: CaskRow, downloader: Downloader, progress: ProgressPort, *, fresh: bool = False
) -> Path | str:
    """Fetch a cask's download.

    Args:
        row: The cask's catalog row.
        downloader: The open downloader.
        progress: The progress sink.
        fresh: Re-download a `no_check` download, as an upgrade must.

    Returns:
        The verified download, or the reason it failed.
    """
    sha = row.sha256 if row.sha256 not in (None, NO_CHECK) else NO_CHECK
    ref = BottleRef(name=row.token, url=str(row.url), sha256=sha)
    if fresh and sha == NO_CHECK:
        # Cached under its URL, which a new version may not change
        downloader.cache_path(ref.key).unlink(missing_ok=True)

    try:
        return await downloader.fetch(
            ref, on_progress=functools.partial(progress.update, row.token, "download")
        )

    except (BrewError, OSError) as exc:
        return str(exc)


async def upgrade_casks(
    names: list[str],
    *,
    repo: Repository,
    backend: UpgradeBackend,
    env: BreweryENV | None = None,
    progress: ProgressPort | None = None,
) -> None:
    """Upgrade every named cask, natively where possible, the rest through brew.

    The downloads overlap and the upgrades run one at a time; a cask whose native
    upgrade fails is restored as it was, and falls back to brew.

    Args:
        names: Installed cask tokens to upgrade.
        repo: The data facade to read the catalog and installed state through.
        backend: The cask backend for the casks brewery can't upgrade.
        env: Brewery environment (paths), resolved if omitted.
        progress: Optional progress sink for the native upgrades.
    """
    env = env or get_brewery_env()
    progress = progress or _NULL_PROGRESS
    opts = read_cask_opts(os.environ.get("HOMEBREW_CASK_OPTS", ""))
    plat = current_platform()
    installed = repo.cache_mgr.installed_packages()
    casks = {p.name for p in installed if p.kind == PackageKind.CASK}
    formulae = {p.name for p in installed if p.kind == PackageKind.FORMULA}

    rows: dict[str, CaskRow] = {}
    reasons: dict[str, str] = {}
    for token in dict.fromkeys(names):
        token_dir = env.caskroom / token
        reasons[token] = classify_uninstall(read_receipt(token_dir), plat, zap=False)
        if (row := repo.catalog.get_cask(token)) is None:
            reasons[token] = reasons[token] or "not in the catalog"
            continue

        if row.language_variations:
            # Upgrade keeps the old config's `explicit` part, over the env's
            languages = [*config_languages(token_dir, ["explicit"]), *opts.languages]
            row = localise(row, [*languages, *system_languages()])

        rows[token] = row
        missing = {f for f in _depends(row, "formula") if f not in formulae}
        reasons[token] = (
            reasons[token] or _brew_reason(row, opts) or _blocked(row, casks, missing)
        )

    native = [t for t, reason in reasons.items() if not reason]
    if native:
        progress.begin(len(native))
        try:
            async with httpx2.AsyncClient(timeout=PIPELINE_TIMEOUT) as client:
                downloader = Downloader(cache_dir=env.bottle_cache, client=client)
                downloads = await asyncio.gather(
                    *(_fetch(rows[t], downloader, progress, fresh=True) for t in native)
                )

            for token, download in zip(native, downloads, strict=True):
                if isinstance(download, str):
                    reasons[token] = download

                else:
                    progress.update(token, "install")
                    try:
                        await asyncio.to_thread(
                            upgrade_cask,
                            rows[token],
                            download,
                            env,
                            dirs=opts.dirs,
                            binaries=opts.binaries,
                            languages=opts.languages,
                        )

                    except (BrewError, OSError) as exc:
                        reasons[token] = str(exc)

                if not reasons[token]:
                    progress.finish(token, Outcome.NATIVE.value)

                else:
                    progress.update(token, "brew")
                    progress.finish(token, Outcome.BREW_UPGRADE.value)

        finally:
            progress.end()

    for token, reason in reasons.items():
        if reason:
            log.info(event="cask_to_brew", cask=token, reason=reason)

    if to_brew := [t for t, reason in reasons.items() if reason]:
        # After the live display ends: a pkg cask may prompt for a sudo password
        await backend.upgrade(names=to_brew)
