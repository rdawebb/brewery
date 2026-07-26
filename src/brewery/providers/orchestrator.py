"""Install + upgrade orchestration: resolve the runtime closure, fetch bottles + manifests
concurrently, and run the native pipeline in dependency order with per-formula
brew fallback.
"""

from __future__ import annotations

import asyncio
import functools
import shutil
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Protocol

from brewery.core.errors import (
    CellarError,
    DownloadError,
    ExtractionError,
    LinkError,
    ManifestError,
    RelocationError,
)
from brewery.core.logging import BreweryLogger, get_logger
from brewery.providers.cellar import install_to_cellar, rmtree
from brewery.providers.downloader import BottleRef, ProgressCb
from brewery.providers.extractor import extract_bottle
from brewery.providers.linker import link_keg, unlink_keg
from brewery.providers.manifest import BottleTabInfo
from brewery.providers.receipt import (
    RuntimeDependency,
    Source,
    build_receipt,
    read_receipt,
    write_receipt,
)
from brewery.providers.relocator import formula_tokens, relocate_keg
from brewery.providers.retention import mark_replaced

log: BreweryLogger = get_logger(__name__)


class FormulaRowP(Protocol):
    """Protocol for interacting with formula rows."""

    name: str
    tap: str | None
    version: str
    revision: int
    version_scheme: int
    keg_only: bool
    post_install: bool
    bottle_url: str
    bottle_sha256: str
    bottle_cellar: str | None
    bottle_rebuild: int


class CatalogPort(Protocol):
    """Protocol for interacting with the formula catalog."""

    def get_formula(self, name: str) -> FormulaRowP | None:
        """Get a formula by name.

        Args:
            name: The name of the formula to retrieve.

        Returns:
            The formula row if found, else None.
        """
        ...

    def resolve_alias(self, name: str) -> str:
        """Resolve a formula alias to its canonical name.

        Args:
            name: The name of the formula to resolve.

        Returns:
            The canonical name of the formula.
        """
        ...

    def runtime_deps(self, name: str) -> list[str]:
        """Direct *runtime* dependency names (deps WHERE kind='runtime').

        Args:
            name: The name of the formula to retrieve.

        Returns:
            The formula row if found, else None.
        """
        ...

    def aliases_of(self, name: str) -> list[str]:
        """Aliases that resolve to this formula (reverse of the alias table).

        Args:
            name: The name of the formula to retrieve.

        Returns:
            The formula row if found, else None.
        """
        ...

    def is_satisfied(self, name: str) -> bool:
        """True if already installed at (at least) the catalog version.

        Args:
            name: The name of the formula to check.

        Returns:
            True if the formula is satisfied, False otherwise.
        """
        ...


class DownloadPort(Protocol):
    """Protocol for interacting with the bottle downloader."""

    async def fetch(
        self, ref: BottleRef, *, on_progress: ProgressCb | None = None
    ) -> Path:
        """Fetch a bottle by its reference.

        Args:
            ref (BottleRef): The reference to the bottle to fetch.
            on_progress: Optional callback fed (downloaded_bytes, total_or_None).

        Returns:
            Path: The path to the downloaded bottle.
        """
        ...


class TabFetcher(Protocol):
    """Protocol for interacting with the bottle tab fetcher."""

    async def __call__(
        self,
        *,
        name: str,
        version: str,
        bottle_sha256: str,
        revision: int,
        rebuild: int,
    ) -> BottleTabInfo:
        """Fetch the bottle tab information for a given formula.

        Args:
            name: The name of the formula.
            version: The version of the formula.
            bottle_sha256: The SHA256 checksum of the bottle.
            revision: The revision number of the formula.
            rebuild: The bottle rebuild counter.

        Returns:
            BottleTabInfo: The bottle tab information.
        """
        ...


class BrewPort(Protocol):
    """Protocol for interacting with Homebrew."""

    async def install(self, name: str) -> bool:
        """Install a formula.

        Args:
            name: The name of the formula to install.

        Returns:
            True if the installation was successful, False otherwise.
        """
        ...

    async def upgrade(self, name: str) -> bool:
        """Upgrade a formula.

        Args:
            name: The name of the formula to upgrade.

        Returns:
            True if the upgrade was successful, False otherwise.
        """
        ...

    async def link(self, name: str) -> bool:
        """Link a formula.

        Args:
            name: The name of the formula to link.

        Returns:
            True if the linking was successful, False otherwise.
        """
        ...

    async def post_install(self, name: str) -> bool:
        """Run post-install steps for a formula.

        Args:
            name: The name of the formula to run post-install steps for.

        Returns:
            True if the post-install steps were successful, False otherwise.
        """
        ...


class ProgressPort(Protocol):
    """Sink for install/upgrade progress events (UI-agnostic).

    Every call happens on the event-loop thread, a no-op default keeps the pipeline
    silent when no reporter is bound.
    """

    def begin(self, total: int) -> None:
        """Signal the start of a transaction installing `total` packages."""
        ...

    def update(
        self,
        name: str,
        stage: str,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        """Report progress for `name` at `stage` ("download" | "install").

        For the download stage, `done`/`total` are byte counts (total may be
        None when the server sent no Content-Length).
        """
        ...

    def finish(self, name: str, outcome: str) -> None:
        """Signal that `name` reached a terminal `Outcome` (by value)."""
        ...

    def end(self) -> None:
        """Tear the reporter down and hand the terminal back."""
        ...


class _NullProgress:
    """No-op ProgressPort; the default when no reporter is bound."""

    def begin(self, total: int) -> None:
        """Ignore the transaction start."""

    def update(
        self,
        name: str,
        stage: str,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        """Ignore a progress update."""

    def finish(self, name: str, outcome: str) -> None:
        """Ignore a package completion."""

    def end(self) -> None:
        """Ignore teardown."""


_NULL_PROGRESS: ProgressPort = _NullProgress()


@dataclass(frozen=True)
class InstallConfig:
    """Configuration for installing a formula."""

    prefix: Path
    repository: Path
    api_path: str  # source.path in the receipt, e.g. <cache>/api/formula.jws.json
    staging_root: Path | None = None  # Defaults to <prefix>/var/homebrew/.staging

    @property
    def cellar(self) -> Path:
        """The Cellar directory for the formula.

        Returns:
            Path: The Cellar directory for the formula.
        """
        return self.prefix / "Cellar"

    def staging(self) -> Path:
        """The staging directory for the formula.

        Returns:
            Path: The staging directory for the formula.
        """
        return self.staging_root or (self.prefix / "var" / "homebrew" / ".staging")


class Outcome(Enum):
    """Possible outcomes of the installation process."""

    NATIVE = "native"  # Installed + linked natively
    NATIVE_KEG_ONLY = "native_keg_only"  # Installed natively, keg-only (not linked)
    BREW_INSTALL = "brew_install"  # Native failed -> brew installed
    BREW_LINK = "brew_link"  # Installed natively, brew did the linking
    BREW_UPGRADE = "brew_upgrade"  # Upgraded via brew
    INSTALLED_UNLINKED = (
        "installed_unlinked"  # Installed, neither we nor brew could link
    )
    SKIPPED_DEP_FAILED = "skipped_dep_failed"
    FAILED = "failed"  # Native and brew both failed


_INSTALLED = {
    Outcome.NATIVE,
    Outcome.NATIVE_KEG_ONLY,
    Outcome.BREW_INSTALL,
    Outcome.BREW_LINK,
    Outcome.INSTALLED_UNLINKED,
}


@dataclass
class InstallReport:
    """Report on the outcome of the installation process.

    `notes` is diagnostic: a formula that fell back to brew successfully still
    records why it fell back; `outcomes` is the sole source of truth for whether
    a formula installed, `errors` is derived from it.
    """

    outcomes: dict[str, Outcome] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)

    def add_note(self, name: str, msg: str) -> None:
        """Record a diagnostic for `name`, keeping any earlier ones.

        Args:
            name: The formula the note is about.
            msg: The diagnostic message.
        """
        self.notes.setdefault(name, []).append(msg)

    @property
    def installed(self) -> list[str]:
        """List of all installed formulae.

        Returns:
            The list of all installed formulae.
        """
        return [n for n, o in self.outcomes.items() if o in _INSTALLED]

    @property
    def failed(self) -> list[str]:
        """List of all failed formulae.

        Returns:
            The list of all failed formulae.
        """
        return [
            n
            for n, o in self.outcomes.items()
            if o in (Outcome.FAILED, Outcome.SKIPPED_DEP_FAILED)
        ]

    @property
    def errors(self) -> dict[str, str]:
        """Notes for the formulae that actually failed.

        Returns:
            Failed formula name -> its notes, joined.
        """
        failed = set(self.failed)

        return {n: "; ".join(m) for n, m in self.notes.items() if n in failed}


@dataclass
class _NativeResult:
    """Result of the in-thread native pipeline for one formula."""

    stage: str | None  # None = success; else 'install' | 'link'
    dest: Path | None = None
    error: str | None = None


class Orchestrator:
    """Orchestrates the installation of formulae."""

    def __init__(
        self,
        *,
        catalog: CatalogPort,
        downloader: DownloadPort,
        tab_fetcher: TabFetcher,
        brew: BrewPort,
        config: InstallConfig,
        install_concurrency: int = 8,
        tab_concurrency: int = 8,
        progress: ProgressPort | None = None,
    ) -> None:
        """Initialises the orchestrator.

        Args:
            catalog: The catalog port.
            downloader: The downloader port.
            tab_fetcher: The tab fetcher.
            brew: The brew port.
            config: The installation configuration.
            install_concurrency: The number of concurrent installations. Defaults to 4.
            tab_concurrency: Maximum concurrent manifest tab fetches. Defaults to 8.
            progress: Optional progress sink; defaults to a no-op reporter.
        """
        self.catalog = catalog
        self.downloader = downloader
        self.tab_fetcher = tab_fetcher
        self.brew = brew
        self.cfg = config
        self.progress = progress or _NULL_PROGRESS
        self._install_sem = asyncio.Semaphore(install_concurrency)
        self._tab_sem = asyncio.Semaphore(tab_concurrency)

    async def install(self, requested: list[str]) -> InstallReport:
        """Installs the requested formulae.

        Args:
            requested: The list of requested formulae.

        Returns:
            InstallReport: The report on the installation outcome.
        """
        req = {self.catalog.resolve_alias(n) for n in requested}
        closure = self._closure(req, include_roots=True)
        to_install = {n for n in closure if not self.catalog.is_satisfied(n)}

        return await self._run_pipeline(to_install, req)

    async def upgrade(
        self, requested: list[str], old_kegs: dict[str, Path]
    ) -> InstallReport:
        """Upgrade installed formulae by re-pouring and swapping each keg.

        The requested targets are forced past is_satisfied, but deps keep normal
        skip-if-present semantics, so only new or missing deps are poured.

        Args:
            requested: Canonical names of the formulae to upgrade.
            old_kegs: Target name -> its current active keg path.

        Returns:
            The InstallReport (per-formula outcomes).
        """
        req = {self.catalog.resolve_alias(n) for n in requested}
        to_install = {
            n
            for n in self._closure(req, include_roots=True)
            if n in req or not self.catalog.is_satisfied(n)
        }

        return await self._run_pipeline(to_install, req, old_kegs)

    async def _run_pipeline(
        self,
        to_install: set[str],
        req: set[str],
        old_kegs: dict[str, Path] | None = None,
    ) -> InstallReport:
        """Fetch concurrently and run the pipeline in dependency order.

        Shared by install and upgrade; old_kegs is empty for install.

        Args:
            to_install: The resolved set of formulae to pour.
            req: The explicitly requested names (sets installed_on_request).
            old_kegs: For upgrades, target -> old keg to unlink and mark.

        Returns:
            The InstallReport.
        """
        report = InstallReport()
        if not to_install:
            return report

        self.progress.begin(len(to_install))

        graph = {
            n: {d for d in self.catalog.runtime_deps(n) if d in to_install}
            for n in to_install
        }
        formulae = {n: self.catalog.get_formula(n) for n in to_install}

        # Start every download up front; installs gate on each one individually
        fetch: dict[str, asyncio.Task] = {
            n: asyncio.create_task(self._fetch(n, formulae[n])) for n in to_install
        }

        ts: TopologicalSorter = TopologicalSorter(graph)
        ts.prepare()
        pending: dict[asyncio.Task, str] = {}

        try:
            while ts.is_active():
                for name in ts.get_ready():
                    task = asyncio.create_task(
                        self._install_one(
                            name, formulae[name], fetch, req, report, old_kegs
                        )
                    )
                    pending[task] = name

                if not pending:
                    break

                done, _ = await asyncio.wait(
                    set(pending), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    name = pending.pop(task)

                    # An unexpected error degrades this formula to FAILED
                    try:
                        outcome = task.result()

                    except asyncio.CancelledError:
                        raise

                    except Exception as exc:
                        log.exception(
                            event="install_task_crashed",
                            formula=name,
                            error=str(exc),
                        )
                        report.add_note(name, f"unexpected error: {exc}")
                        outcome = Outcome.FAILED

                    report.outcomes[name] = outcome
                    self.progress.finish(name, outcome.value)
                    ts.done(name)

        finally:
            for t in fetch.values():
                t.cancel()

            await asyncio.gather(*fetch.values(), return_exceptions=True)

            # Drain any pending tasks
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            self.progress.end()

        return report

    def _closure(self, names: Iterable[str], *, include_roots: bool) -> set[str]:
        """The transitive runtime-dependency closure of `names`.

        Args:
            names: The formula names to walk from; aliases are resolved.
            include_roots: Whether the roots themselves are part of the result.
                When False a root still appears if it is genuinely its own
                transitive dependency.

        Returns:
            The set of canonical names in the closure.
        """
        roots = [self.catalog.resolve_alias(n) for n in names]
        stack = (
            list(roots)
            if include_roots
            else [d for r in roots for d in self.catalog.runtime_deps(r)]
        )

        seen: set[str] = set()
        while stack:
            name = self.catalog.resolve_alias(stack.pop())
            if name in seen:
                continue

            seen.add(name)
            stack.extend(self.catalog.runtime_deps(name))

        return seen

    def _runtime_dep_entries(self, name: str) -> list[RuntimeDependency]:
        """Receipt `runtime_dependencies` built from the installed closure.

        Args:
            name: The canonical formula name whose receipt entries to build.

        Returns:
            A list of `RuntimeDependency` entries for every transitive runtime
            dependency, sorted by name, with `declared_directly` set for
            direct deps.
        """
        direct = set(self.catalog.runtime_deps(name))
        entries: list[RuntimeDependency] = []
        for dep in sorted(self._closure([name], include_roots=False)):
            dfr = self.catalog.get_formula(dep)
            if dfr is None:
                continue

            pkg_version = (
                f"{dfr.version}_{dfr.revision}" if dfr.revision else dfr.version
            )

            entries.append(
                RuntimeDependency(
                    full_name=dep,
                    version=dfr.version,
                    revision=dfr.revision,
                    bottle_rebuild=dfr.bottle_rebuild,
                    pkg_version=pkg_version,
                    declared_directly=dep in direct,
                )
            )

        return entries

    async def _fetch(
        self, name: str, fr: FormulaRowP | None
    ) -> tuple[Path | None, BottleTabInfo | None, str | None, str | None]:
        """Download bottle + manifest concurrently. Returns (bottle_path, tab).

        bottle_path is None if the formula has no bottle (forces brew fallback);
        tab is None if the manifest is unavailable (also forces brew fallback,
        since a faithful receipt needs it).

        Args:
            name: The name of the formula.
            fr: The formula row information.

        Returns:
            A tuple containing bottle path, tab, bottle error, and tab error information.
        """
        if fr is None or fr.bottle_url is None or fr.bottle_sha256 is None:
            return None, None, "no bottle in catalog", "no bottle in catalog"

        ref = BottleRef(name, fr.bottle_url, fr.bottle_sha256)
        tab_error: str | None = None
        bottle_error: str | None = None

        async def _tab() -> BottleTabInfo | None:
            """Fetch the bottle tab information.

            Returns:
                The bottle tab information, or None if not available.
            """
            nonlocal tab_error
            try:
                async with self._tab_sem:
                    return await self.tab_fetcher(
                        name=name,
                        version=fr.version,
                        bottle_sha256=fr.bottle_sha256,
                        revision=fr.revision,
                        rebuild=fr.bottle_rebuild,
                    )

            except ManifestError as e:
                tab_error = str(e)
                return None

        async def _bottle() -> Path | None:
            """Fetch the bottle information.

            Returns:
                The bottle information, or None if not available.
            """
            nonlocal bottle_error
            try:
                return await self.downloader.fetch(
                    ref,
                    on_progress=functools.partial(
                        self.progress.update, name, "download"
                    ),
                )

            except DownloadError as e:
                bottle_error = str(e)
                return None

        bottle_path, tab = await asyncio.gather(_bottle(), _tab())

        return bottle_path, tab, bottle_error, tab_error

    async def _install_one(
        self,
        name: str,
        fr: FormulaRowP | None,
        fetch: dict[str, asyncio.Task],
        requested: set[str],
        report: InstallReport,
        old_kegs: dict[str, Path] | None = None,
    ) -> Outcome:
        """Install a single formula.

        Args:
            name: The name of the formula.
            fr: The formula row information.
            fetch: The fetch tasks for the formula.
            requested: The set of requested formulae.
            report: The InstallReport.
            old_kegs: Optional paths of the old kegs to be removed.

        Returns:
            The outcome of the installation.
        """
        outcomes = report.outcomes
        old = (old_kegs or {}).get(name)

        # Skip if any runtime dep failed outright
        deps = self.catalog.runtime_deps(name)
        failed_deps = [
            d
            for d in deps
            if outcomes.get(d) in (Outcome.FAILED, Outcome.SKIPPED_DEP_FAILED)
        ]

        if failed_deps:
            fetch[name].cancel()
            report.add_note(
                name, f"dependency failed: {', '.join(sorted(failed_deps))}"
            )

            return Outcome.SKIPPED_DEP_FAILED

        bottle_path, tab, bottle_error, tab_error = await fetch[name]

        # No bottle, no tab, or unknown formula -> brew owns this one.
        if fr is None or bottle_path is None or tab is None:
            reason = (
                "unknown formula"
                if fr is None
                else f"bottle download failed: {bottle_error}"
                if bottle_path is None
                else f"manifest tab unavailable: {tab_error}"
            )
            report.add_note(name, f"{reason} -> brew")

            return await self._brew_pour(name, upgrade=old is not None)

        on_request = name in requested
        if old is not None:
            prev = read_receipt(old)
            if prev is not None:
                on_request = bool(prev.get("installed_on_request", on_request))

        aliases = self.catalog.aliases_of(name)
        rt_deps = self._runtime_dep_entries(name)

        async with self._install_sem:
            self.progress.update(name, "install")
            result = await asyncio.to_thread(
                self._native_install,
                name,
                fr,
                bottle_path,
                tab,
                on_request,
                aliases,
                rt_deps,
                old,
            )

        if old is not None and result.stage != "install":
            await asyncio.to_thread(mark_replaced, old, by=fr.version)

        if result.stage is None:
            if fr.post_install:
                await self.brew.post_install(name)  # Best-effort hook

            return Outcome.NATIVE_KEG_ONLY if fr.keg_only else Outcome.NATIVE

        if result.stage == "link":
            report.add_note(name, f"link stage: {result.error}")

            # Native install succeeded but linking didn't: try brew link, then
            # leave installed-but-unlinked (brew's own behaviour)
            if await self.brew.link(name):
                return Outcome.BREW_LINK

            return Outcome.INSTALLED_UNLINKED

        # An install-stage failure -> brew pours this formula
        report.add_note(name, f"install stage: {result.error}")

        return await self._brew_pour(name, upgrade=old is not None)

    async def _brew_pour(self, name: str, *, upgrade: bool) -> Outcome:
        """Brew fallback: `brew upgrade` for an upgrade target, else `brew install`.

        Args:
            name: The name of the formula.
            upgrade: Whether this is an upgrade operation.

        Returns:
            The outcome of the installation.
        """
        ok = await (self.brew.upgrade(name) if upgrade else self.brew.install(name))
        if not ok:
            return Outcome.FAILED

        return Outcome.BREW_UPGRADE if upgrade else Outcome.BREW_INSTALL

    async def _brew_install(self, name: str) -> Outcome:
        """Install a formula using Homebrew.

        Args:
            name: The name of the formula.

        Returns:
            The outcome of the installation.
        """
        ok = await self.brew.install(name)

        return Outcome.BREW_INSTALL if ok else Outcome.FAILED

    def _native_install(
        self,
        name: str,
        fr: FormulaRowP,
        bottle_path: Path,
        tab: BottleTabInfo,
        on_request: bool,
        aliases: list[str],
        rt_deps: list[RuntimeDependency],
        old_keg: Path | None = None,
    ) -> _NativeResult:
        """Install a formula natively.

        Args:
            name: The name of the formula.
            fr: The formula row information.
            bottle_path: The path to the bottle file.
            tab: The bottle tab information.
            on_request: Whether the installation was triggered by a user request.
            aliases: Known aliases for the formula, written into the receipt.
            rt_deps: Pre-built runtime dependency entries for the receipt.
            old_keg: Optional path of the old keg to be removed.

        Returns:
            The result of the installation.
        """
        pkg_version = f"{fr.version}_{fr.revision}" if fr.revision else fr.version
        staging_root = self.cfg.staging()
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=staging_root))

        dest: Path | None = None
        try:
            keg = extract_bottle(bottle_path, staging)
            relocate_keg(
                keg,
                prefix=self.cfg.prefix,
                cellar=self.cfg.cellar,
                repository=self.cfg.repository,
                skip_relocation=(fr.bottle_cellar == ":any_skip_relocation"),
                extra_tokens=formula_tokens(
                    self.cfg.prefix,
                    name=name,
                    runtime_deps=rt_deps,
                    built_on=tab.built_on,
                ),
                text_files=tab.changed_files,
            )
            dest = install_to_cellar(
                keg, prefix=self.cfg.prefix, name=name, version=pkg_version
            )
            write_receipt(
                dest, self._build_receipt(name, fr, tab, on_request, aliases, rt_deps)
            )

        except (ExtractionError, RelocationError, CellarError, OSError) as exc:
            if dest is not None:
                self._cleanup_partial(dest, name)

            return _NativeResult(stage="install", error=str(exc))

        finally:
            shutil.rmtree(staging, ignore_errors=True)

        try:
            if old_keg is not None and old_keg != dest:
                unlink_keg(old_keg, prefix=self.cfg.prefix, name=name)

            link_keg(dest, prefix=self.cfg.prefix, name=name, keg_only=fr.keg_only)

        except (LinkError, OSError) as exc:
            return _NativeResult(stage="link", dest=dest, error=str(exc))

        return _NativeResult(stage=None, dest=dest)

    def _cleanup_partial(self, dest: Path, name: str) -> None:
        """Remove a keg left in the Cellar by a receipt-stage failure, so brew can re-try.

        Args:
            dest: The destination path of the keg.
            name: The name of the formula.
        """
        if dest.exists():
            rmtree(dest)

        # Resolves relative to actual Cellar location
        opt: Path = dest.parents[2] / "opt" / name

        # Only remove opt if it dangles
        if opt.is_symlink() and not opt.exists():
            opt.unlink()

    def _build_receipt(
        self,
        name: str,
        fr: FormulaRowP,
        tab: BottleTabInfo,
        on_request: bool,
        aliases: list[str],
        rt_deps: list[RuntimeDependency],
    ) -> dict:
        """Build a receipt for the installed formula.

        Args:
            name: The name of the formula.
            fr: The formula row information.
            tab: The bottle tab information.
            on_request: Whether the installation was triggered by a user request.
            aliases: Known aliases for the formula.
            rt_deps: Pre-built runtime dependency entries.

        Returns:
            A dictionary representing the receipt.
        """
        return build_receipt(
            homebrew_version=tab.homebrew_version,
            changed_files=tab.changed_files,
            source_modified_time=tab.source_modified_time,
            compiler=tab.compiler,
            runtime_dependencies=rt_deps,
            built_on=tab.built_on,
            installed_on_request=on_request,
            time=int(time.time()),
            source=Source(
                stable_version=fr.version,
                api_path=self.cfg.api_path,
                version_scheme=fr.version_scheme,
                tap=fr.tap or "homebrew/core",
            ),
            aliases=aliases,
        )
