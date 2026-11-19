"""Cask operations: handled natively where possible, otherwise by brew."""

from __future__ import annotations

import asyncio
import functools
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import httpx2

from brewery.core.cask_locale import config_languages, localise, system_languages
from brewery.core.catalog import CaskRow
from brewery.core.config import BreweryENV, get_brewery_env
from brewery.core.errors import (
    AlreadyInstalledWarning,
    BrewError,
    OperationInProgressError,
)
from brewery.core.host import current_platform
from brewery.core.locks import cask_lock
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import Notes, PackageKind
from brewery.core.shell import run_brew
from brewery.core.sudo import validate
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
    git_files,
    needs_sudo,
    read_cask_opts,
    url_headers,
)
from brewery.providers.caskroom import (
    Download,
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
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from pathlib import Path

    from brewery.core.repo import Repository

log: BreweryLogger = get_logger(name=__name__)

# Matching native install concurrency limit
_BREW_CONCURRENCY = 8


async def install_casks(
    names: list[str],
    *,
    repo: Repository,
    backend: InstallBackend,
    formula: PackageBackend,
    env: BreweryENV | None = None,
    progress: ProgressPort | None = None,
) -> Notes:
    """Install every named cask, natively where possible, the rest through brew.

    Casks already installed are skipped. A cask's `depends_on cask` is installed
    natively first, and its `depends_on formula` through the formula pipeline; a
    cask whose native install or dependencies fail goes to brew, which then installs
    those dependencies; a cask another process has locked is left alone.

    Args:
        names: Cask tokens to install.
        repo: The data facade to read the catalog and installed state through.
        backend: The cask backend for the casks brewery can't install.
        formula: Formula backend for the formula pipeline's brew fallback.
        env: Brewery environment (paths), resolved if omitted.
        progress: Optional progress sink.

    Returns:
        A (token, reason) note per cask left alone because it was locked, or
        that brew failed to install.
    """
    env = env or get_brewery_env()
    progress = progress or _NULL_PROGRESS
    opts = read_cask_opts(os.environ.get("HOMEBREW_CASK_OPTS", ""))
    notes: Notes = []
    installed = {p.name for p in repo.cache_mgr.installed_packages(PackageKind.CASK)}
    requested = [n for n in dict.fromkeys(names) if n not in installed]

    rows: dict[str, CaskRow] = {}
    order: list[str] = []  # Dependencies before their dependents

    def visit(token: str) -> None:
        """Visit a cask and its dependencies, recording the order to install them in."""
        if token in rows or token in installed:
            return

        if (row := repo.catalog.get_cask(token)) is None:
            return  # brew reports the unknown name

        if row.language_variations:
            row = localise(row, [*opts.languages, *system_languages()])

        rows[token] = row
        for dep in _depends(row, "cask"):
            visit(dep)
        order.append(token)

    for token in requested:
        visit(token)

    reasons = {t: _brew_reason(rows[t], opts) for t in order}
    if any(reasons[t] and needs_sudo(rows[t].artifacts) for t in order):
        await validate()  # Before any display, rather than mid-way through it

    native = [t for t in order if not reasons[t]]
    failed_formulae = await _install_formulae(
        sorted({f for t in native for f in _depends(rows[t], "formula")}),
        repo=repo,
        formula=formula,
        env=env,
        progress=progress,
    )

    done = set(installed)

    # A row per native cask, and per requested one falling back to brew
    total = len({*native, *requested})
    async with _downloads(env, progress, total) as (downloader, fetches):
        fetches.extend(
            asyncio.create_task(_fetch(rows[t], downloader, progress)) for t in native
        )

        # In dependency order, each as soon as its download finishes
        for token, fetch in zip(native, fetches, strict=True):
            row = rows[token]
            try:
                reasons[token] = _blocked(row, done, failed_formulae) or (
                    await _install_one(
                        row,
                        await fetch,
                        env,
                        opts,
                        progress,
                        on_request=token in requested,
                    )
                )

            except OperationInProgressError as exc:
                _note_locked(token, exc, notes, progress)
                continue

            if not reasons[token]:
                done.add(token)
                progress.finish(token, Outcome.NATIVE.value)

            elif token not in requested:
                # Left to the brew call of the cask that depends on it
                log.info(event="cask_to_brew", cask=token, reason=reasons[token])
                progress.finish(token, Outcome.FAILED.value)

        held = {t for t, _ in notes}
        sem = asyncio.Semaphore(_BREW_CONCURRENCY)
        brewing: dict[str, asyncio.Task] = {}

        async def via_brew(token: str) -> None:
            """Install one cask through brew, after any it depends on."""
            deps = _cask_deps(token, rows)
            if token in deps:
                notes.append((token, "dependency cycle"))
                progress.finish(token, Outcome.FAILED.value)
                return

            # brew would install a missing dependency itself, racing its own call
            for dep in deps & brewing.keys():
                await brewing[dep]

            async with sem:
                await _brew_one(
                    token,
                    reasons.get(token, "not in the catalog"),
                    functools.partial(backend.install, names=[token]),
                    Outcome.BREW_INSTALL,
                    progress,
                    notes,
                )

        # A bug in one cancels the rest, rather than leaving brew calls hanging
        async with asyncio.TaskGroup() as tg:
            for token in requested:
                if token not in done and token not in held:
                    brewing[token] = tg.create_task(via_brew(token))

    return notes


@asynccontextmanager
async def _downloads(
    env: BreweryENV, progress: ProgressPort, total: int
) -> AsyncGenerator[tuple[Downloader, list[asyncio.Task]]]:
    """Open the progress display and a downloader for a run of native casks.

    Args:
        env: Brewery environment, for the download cache.
        progress: The progress sink.
        total: How many casks the run covers.

    Yields:
        The downloader, and a list for the caller's fetch tasks: any still
        running when the block exits are cancelled, and waited for.
    """
    progress.begin(total)
    tasks: list[asyncio.Task] = []
    try:
        async with httpx2.AsyncClient(timeout=PIPELINE_TIMEOUT) as client:
            try:
                yield Downloader(cache_dir=env.bottle_cache, client=client), tasks

            finally:
                for t in tasks:
                    t.cancel()

                await asyncio.gather(*tasks, return_exceptions=True)

    finally:
        progress.end()


async def _brew_one(
    token: str,
    reason: str,
    run: Callable[[], Awaitable[object]],
    outcome: Outcome,
    progress: ProgressPort,
    notes: Notes,
) -> None:
    """Hand one cask to brew in its own row, noting it if brew fails.

    Args:
        token: The cask token.
        reason: Why it goes to brew, for the log.
        run: Runs brew on the cask.
        outcome: The outcome to report once brew succeeds.
        progress: The progress sink.
        notes: The notes a brew failure is added to.
    """
    log.info(event="cask_to_brew", cask=token, reason=reason)
    progress.update(token, "brew")
    try:
        await run()

    except AlreadyInstalledWarning:
        pass  # Installed since last check: success, as in `BrewAdapter.install`

    except BrewError as exc:
        notes.append((token, _brew_note(exc)))
        progress.finish(token, Outcome.FAILED.value)
        return

    progress.finish(token, outcome.value)


def _cask_deps(token: str, rows: dict[str, CaskRow]) -> set[str]:
    """Every cask `token` depends on, directly or not, among `rows`.

    Args:
        token: The cask token.
        rows: The catalog rows of the run's casks.

    Returns:
        The dependencies' tokens.
    """
    found: set[str] = set()
    stack = [token]
    while stack:
        if row := rows.get(stack.pop()):
            new = set(_depends(row, "cask")) - found
            found |= new
            stack.extend(new)

    return found


def _brew_note(exc: BrewError) -> str:
    """The last line of brew's error output, as a cask's failure note.

    Args:
        exc: brew's failure.

    Returns:
        brew's last error line, or the exception's message with no output.
    """
    lines = str(exc.context.get("error", "")).strip().splitlines()

    return lines[-1] if lines else exc.message


def _note_locked(
    token: str, exc: OperationInProgressError, locked: Notes, progress: ProgressPort
) -> None:
    """Record a cask another process has locked.

    Args:
        token: The cask token.
        exc: The contended lock's error.
        locked: The notes to add it to.
        progress: The progress sink.
    """
    log.warning(event="cask_locked", cask=token, error=str(exc))
    locked.append((token, str(exc)))
    progress.finish(token, Outcome.FAILED.value)


def _locked(token: str, env: BreweryENV, change: Callable[[], None]) -> None:
    """Make one cask's Caskroom and artifact changes under its lock.

    Args:
        token: The cask token.
        env: Brewery environment.
        change: The changes.

    Raises:
        OperationInProgressError: Another brewery or `brew cleanup` holds the lock.
    """
    with cask_lock(token, prefix=env.prefix):
        change()


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

    return classify(row, current_platform())


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
    download: Download | str,
    env: BreweryENV,
    opts: CaskOpts,
    progress: ProgressPort,
    *,
    on_request: bool,
) -> str:
    """Stage and install one cask's download, unstaging it if any step fails.

    Args:
        row: The cask's catalog row.
        download: Its download, or the reason the fetch failed.
        env: Brewery environment.
        opts: The parsed `HOMEBREW_CASK_OPTS`.
        progress: The progress sink.
        on_request: Whether the user asked for it, rather than a dependent cask.

    Returns:
        The reason it failed, or an empty string once it's installed.

    Raises:
        OperationInProgressError: Another process has locked the cask.
    """
    if isinstance(download, str):
        return download

    try:
        progress.update(row.token, "install")
        await asyncio.to_thread(
            _locked,
            row.token,
            env,
            functools.partial(_stage_and_install, row, download, env, opts, on_request),
        )

    except OperationInProgressError:
        raise

    except (BrewError, OSError) as exc:
        return str(exc)

    return ""


def _stage_and_install(
    row: CaskRow, download: Download, env: BreweryENV, opts: CaskOpts, on_request: bool
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
) -> Notes:
    """Uninstall every named cask, natively where possible, the rest through brew.

    A cask whose native uninstall fails part way goes to brew, which retries it; a
    cask another process has locked is left alone.

    Args:
        names: Cask tokens to uninstall.
        backend: The cask backend for the casks brewery can't uninstall.
        env: Brewery environment (paths), resolved if omitted.
        zap: Also remove each cask's `zap` files, as `--zap` does.

    Returns:
        A (token, reason) note per cask left alone because it was locked.
    """
    env = env or get_brewery_env()
    plat = current_platform()
    to_brew: list[str] = []
    locked: Notes = []
    sudo = False

    for token in names:
        receipt = read_receipt(env.caskroom / token)
        reason = classify_uninstall(receipt, plat, zap=zap)

        if not reason:
            try:
                await asyncio.to_thread(
                    _locked,
                    token,
                    env,
                    functools.partial(uninstall_cask, token, env, zap=zap),
                )
                continue

            except OperationInProgressError as exc:
                _note_locked(token, exc, locked, _NULL_PROGRESS)
                continue

            except (BrewError, OSError) as exc:
                reason = str(exc)

        log.info(event="cask_to_brew", cask=token, reason=reason)
        to_brew.append(token)
        sudo = sudo or needs_sudo((receipt or {}).get("uninstall_artifacts"))

    if to_brew:
        if sudo:
            await validate()

        await backend.uninstall(names=to_brew, zap=zap)

    return locked


async def _fetch(
    row: CaskRow, downloader: Downloader, progress: ProgressPort, *, fresh: bool = False
) -> Download | str:
    """Fetch a cask's download, or each file of its git checkout.

    Args:
        row: The cask's catalog row.
        downloader: The open downloader.
        progress: The progress sink.
        fresh: Re-download a `no_check` download, as an upgrade must.

    Returns:
        The verified download, or the reason it failed.
    """
    if files := git_files(row):
        got = await asyncio.gather(
            *(
                _fetch_one(
                    BottleRef(row.token, url, NO_CHECK),
                    downloader,
                    progress,
                    fresh=fresh,
                )
                for url in files.values()
            )
        )
        if (failed := next((g for g in got if isinstance(g, str)), None)) is not None:
            return failed

        return cast("dict[str, Path]", dict(zip(files, got, strict=True)))

    ref = BottleRef(
        row.token, str(row.url), row.sha256 or NO_CHECK, url_headers(row.url_specs)
    )

    return await _fetch_one(ref, downloader, progress, fresh=fresh)


async def _fetch_one(
    ref: BottleRef, downloader: Downloader, progress: ProgressPort, *, fresh: bool
) -> Path | str:
    """Fetch one file.

    Args:
        ref: The file, named for the cask.
        downloader: The open downloader.
        progress: The progress sink.
        fresh: Re-download it if it's `no_check`.

    Returns:
        The verified file, or the reason it failed.
    """
    if fresh and ref.sha256 == NO_CHECK:
        # Cached under its URL, which a new version may not change
        downloader.cache_path(ref.key).unlink(missing_ok=True)

    try:
        return await downloader.fetch(
            ref, on_progress=functools.partial(progress.update, ref.name, "download")
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
) -> Notes:
    """Upgrade every named cask, natively where possible, the rest through brew.

    The downloads overlap and the upgrades run one at a time, in the order the
    downloads finish; a cask whose native upgrade fails is restored as it was, and
    falls back to brew; a cask another process has locked is left alone.

    Args:
        names: Installed cask tokens to upgrade.
        repo: The data facade to read the catalog and installed state through.
        backend: The cask backend for the casks brewery can't upgrade.
        env: Brewery environment (paths), resolved if omitted.
        progress: Optional progress sink.

    Returns:
        A (token, reason) note per cask left alone because it was locked, or
        that brew failed to upgrade.
    """
    env = env or get_brewery_env()
    progress = progress or _NULL_PROGRESS
    opts = read_cask_opts(os.environ.get("HOMEBREW_CASK_OPTS", ""))
    plat = current_platform()
    notes: Notes = []
    installed = repo.cache_mgr.installed_packages()
    casks = {p.name for p in installed if p.kind == PackageKind.CASK}
    formulae = {p.name for p in installed if p.kind == PackageKind.FORMULA}

    rows: dict[str, CaskRow] = {}
    reasons: dict[str, str] = {}
    sudo: set[str] = set()  # brew removes the old version, then installs new one

    for token in dict.fromkeys(names):
        token_dir = env.caskroom / token
        receipt = read_receipt(token_dir)
        reasons[token] = classify_uninstall(receipt, plat, zap=False)
        if needs_sudo((receipt or {}).get("uninstall_artifacts")):
            sudo.add(token)

        if (row := repo.catalog.get_cask(token)) is None:
            reasons[token] = reasons[token] or "not in the catalog"
            continue

        if row.language_variations:
            # Upgrade keeps the old config's `explicit` part, over the env's
            languages = [*config_languages(token_dir, ["explicit"]), *opts.languages]
            row = localise(row, [*languages, *system_languages()])

        rows[token] = row
        if needs_sudo(row.artifacts):
            sudo.add(token)

        missing = {f for f in _depends(row, "formula") if f not in formulae}
        reasons[token] = (
            reasons[token] or _brew_reason(row, opts) or _blocked(row, casks, missing)
        )

    if any(reasons[t] for t in sudo):
        await validate()  # Before the display, rather than mid-way through

    async with _downloads(env, progress, len(reasons)) as (downloader, fetches):
        if native := [t for t, reason in reasons.items() if not reason]:

            async def tagged(token: str) -> tuple[str, Download | str]:
                """Fetch a cask's download, returning its token with it."""
                return token, await _fetch(
                    rows[token], downloader, progress, fresh=True
                )

            fetches.extend(asyncio.create_task(tagged(t)) for t in native)

            # No order between upgrades, so each as soon as its download lands
            for next_done in asyncio.as_completed(fetches):
                token, download = await next_done
                if isinstance(download, str):
                    reasons[token] = download
                    continue

                progress.update(token, "install")
                try:
                    await asyncio.to_thread(
                        _locked,
                        token,
                        env,
                        functools.partial(
                            upgrade_cask,
                            rows[token],
                            download,
                            env,
                            dirs=opts.dirs,
                            binaries=opts.binaries,
                            languages=opts.languages,
                        ),
                    )

                except OperationInProgressError as exc:
                    _note_locked(token, exc, notes, progress)
                    continue

                except (BrewError, OSError) as exc:
                    reasons[token] = str(exc)
                    continue

                progress.finish(token, Outcome.NATIVE.value)

        sem = asyncio.Semaphore(_BREW_CONCURRENCY)

        async def via_brew(token: str, reason: str) -> None:
            """Upgrade one cask through brew."""
            async with sem:
                await _brew_one(
                    token,
                    reason,
                    functools.partial(backend.upgrade, names=[token]),
                    Outcome.BREW_UPGRADE,
                    progress,
                    notes,
                )

        await asyncio.gather(*(via_brew(t, r) for t, r in reasons.items() if r))

    return notes
