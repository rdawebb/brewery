"""Tests for the install orchestrator.

Every external dependency is a Mock (catalog, downloader,tab fetcher, brew), and
the in-thread native pipeline is replaced with ascripted stub so the tests exercise
the *scheduling and fallback* logic withoutreal bottles or a macOS filesystem. The
one exception is _cleanup_partial, whichis tested directly against a real temp Cellar.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from brewery.providers.downloader import BottleRef, DownloadError
from brewery.providers.manifest import BottleTabInfo, ManifestError
from brewery.providers.orchestrator import (
    InstallConfig,
    Orchestrator,
    Outcome,
    _NativeResult,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

CFG = InstallConfig(prefix=Path("/opt/hb"), repository=Path("/opt/hb"), api_path="/api")


def _tab(name: str = "x") -> BottleTabInfo:
    """Create a mock BottleTabInfo for testing.

    Args:
        name: The name of the formula.
        version: The version of the formula.
        bottle_sha256: The SHA256 checksum of the bottle.
        revision: The revision number of the formula.

    Returns:
        BottleTabInfo: The bottle tab information.
    """
    return BottleTabInfo(
        homebrew_version="5.1",
        changed_files=[],
        source_modified_time=1,
        compiler="clang",
        runtime_dependencies=[],
        arch="x86_64",
        built_on={"os": "Macintosh"},
        path_exec_files=[],
        installed_size=None,
    )


class MockFormula:
    """Mock formula class for testing."""

    def __init__(
        self, name, keg_only=False, post_install=False, has_bottle=True
    ) -> None:
        """Initialises a mock formula.

        Args:
            name: The name of the formula.
            keg_only: Whether the formula is keg-only.
            post_install: Whether the formula has post-install steps.
            has_bottle: Whether the formula has a bottle.
        """
        self.name = name
        self.tap = "homebrew/core"
        self.version = "1.0"
        self.revision = 0
        self.version_scheme = 0
        self.keg_only = keg_only
        self.post_install = post_install
        self.bottle_url = f"https://ghcr.io/{name}" if has_bottle else None
        self.bottle_sha256 = f"sha_{name}" if has_bottle else None
        self.bottle_cellar = ":any"
        self.bottle_rebuild = 0


class MockCatalog:
    """Mock catalog class for testing."""

    def __init__(self, formulae, deps, satisfied=(), aliases=None) -> None:
        """Initialises a mock catalog.

        Args:
            formulae: The formulae to include in the catalog.
            deps: The dependencies for each formula.
            satisfied: The formulae that are already satisfied.
            aliases: The aliases for each formula.
        """
        self._f = formulae
        self._deps = deps
        self._sat = set(satisfied)
        self._al = aliases or {}

    def get_formula(self, name: str):
        """Gets a formula by name.

        Args:
            name: The name of the formula.

        Returns:
            The mock formula.
        """
        return self._f.get(name)

    def resolve_alias(self, name: str) -> str:
        """Resolves an alias to a formula name.

        Args:
            name: The name of the formula.

        Returns:
            The resolved formula name.
        """
        return name

    def runtime_deps(self, name: str) -> list[str]:
        """Gets the runtime dependencies for a formula.

        Args:
            name: The name of the formula.

        Returns:
            The runtime dependencies for the formula.
        """
        return list(self._deps.get(name, []))

    def aliases_of(self, name: str) -> list[str]:
        """Gets the aliases for a formula.

        Args:
            name: The name of the formula.

        Returns:
            The aliases for the formula.
        """
        return self._al.get(name, [])

    def is_satisfied(self, name: str) -> bool:
        """Checks if a formula is satisfied.

        Args:
            name: The name of the formula.

        Returns:
            True if the formula is satisfied, False otherwise.
        """
        return name in self._sat


class MockDownloader:
    """Mock downloader class for testing."""

    def __init__(self, delays=None) -> None:
        """Initialises the mock downloader.

        Args:
            delays: A dictionary mapping formula names to download delays.
        """
        self.delays = delays or {}
        self.done_order = []

    async def fetch(self, ref: BottleRef) -> Path:
        """Fetches a bottle file.

        Args:
            ref: The bottle reference.

        Returns:
            The path to the downloaded bottle file.
        """
        await asyncio.sleep(self.delays.get(ref.name, 0))
        self.done_order.append(ref.name)

        return Path(f"/Mock/{ref.name}.tar.gz")


class FailingDownloader:
    """Mock downloader class that always fails."""

    async def fetch(self, ref: BottleRef) -> Path:
        """Fetch function that always fails.

        Args:
            ref: The bottle reference.

        Raises:
            DownloadError: Mocked download error.
        """
        raise DownloadError(ref, "boom")


class MockTab:
    """Mock tab class for testing."""

    def __init__(self, fail=()) -> None:
        """Initialises the mock tab.

        Args:
            fail: A list of formula names that should fail.
        """
        self.fail = set(fail)

    async def __call__(self, *, name, version, bottle_sha256, revision):
        """Calls the mock tab.

        Args:
            name: The name of the formula.
            version: The version of the formula.
            bottle_sha256: The SHA256 checksum of the bottle.
            revision: The revision of the formula.

        Returns:
            The tab for the formula.
        """
        if name in self.fail:
            raise ManifestError("no tab")

        return _tab(name)


class MockBrew:
    """Mock brew class for testing."""

    def __init__(self, install_ok=True, link_ok=True) -> None:
        """Initialises the mock brew.

        Args:
            install_ok: Whether the install should succeed.
            link_ok: Whether the link should succeed.
        """
        self.install_ok = install_ok
        self.link_ok = link_ok
        self.calls = []

    async def install(self, name) -> bool:
        """Installs a formula.

        Args:
            name: The name of the formula.

        Returns:
            True if the installation succeeded, False otherwise.
        """
        self.calls.append(("install", name))
        return self.install_ok

    async def link(self, name) -> bool:
        """Links a formula.

        Args:
            name: The name of the formula.

        Returns:
            True if the linking succeeded, False otherwise.
        """
        self.calls.append(("link", name))
        return self.link_ok

    async def post_install(self, name) -> bool:
        """Mock post-installation that always succeeds.

        Args:
            name: The name of the formula.

        Returns:
            A boolean indicating success.
        """
        self.calls.append(("postinstall", name))
        return True


def _make(cat, dl, tabf, brew, native=None, order=None):
    """Create an orchestrator with mocks and optional native install tracking.

    Args:
        cat: The formula catalog.
        dl: The downloader.
        tabf: The tab fetcher.
        brew: The brew manager.
        native: Optional native install tracking.
        order: Optional list to track installation order.

    Returns:
        An instance of the orchestrator.
    """
    o = Orchestrator(
        catalog=cat, downloader=dl, tab_fetcher=tabf, brew=brew, config=CFG
    )
    if native is not None:

        def mock_native(name, fr, bottle_path, tab, on_request):
            """Mock native installation function.

            Args:
                name: The name of the formula.
                fr: The formula reference.
                bottle_path: The path to the bottle.
                tab: The tab for the formula.
                on_request: The request callback.

            Returns:
                The result of the native installation.
            """
            if order is not None:
                order.append(name)

            return native.get(
                name, _NativeResult(stage=None, dest=Path(f"/opt/hb/Cellar/{name}/1.0"))
            )

        o._native_install = mock_native  # ty: ignore[invalid-assignment]

    return o


def _graph(deps) -> dict[str, MockFormula]:
    """Create a graph of formula dependencies.

    Args:
        deps: A dictionary mapping formula names to their dependencies.

    Returns:
        A dictionary mapping formula names to their mock formula objects.
    """
    return {n: MockFormula(n) for n in deps}


class TestSchedulingOrdering:
    """Tests for scheduling and ordering of installations."""

    async def test_installs_in_dependency_order(self) -> None:
        """Tests that installations occur in the order dictated by their dependencies."""
        deps = {"app": ["libA", "libB"], "libA": ["libC"], "libB": [], "libC": []}
        cat = MockCatalog(_graph(deps), deps)
        order = []
        o = _make(cat, MockDownloader(), MockTab(), MockBrew(), native={}, order=order)
        report = await o.install(["app"])
        assert all(v is Outcome.NATIVE for v in report.outcomes.values())
        assert order.index("libC") < order.index("libA") < order.index("app")
        assert order.index("libB") < order.index("app")

    async def test_leaf_dep_installs_while_large_keg_downloads(self) -> None:
        """Tests that leaf dependencies are installed while large keg downloads are in progress."""
        deps = {"big": ["small"], "small": []}
        cat = MockCatalog(_graph(deps), deps)
        dl = MockDownloader(delays={"big": 0.2, "small": 0.0})
        order = []
        o = _make(cat, dl, MockTab(), MockBrew(), native={}, order=order)
        await o.install(["big"])

        # Small downloads + installs before big's download finishes
        assert order == ["small", "big"]
        assert dl.done_order == ["small", "big"]

    async def test_satisfied_deps_are_dropped(self) -> None:
        """Tests that satisfied dependencies are dropped from the installation plan."""
        deps = {"app": ["dep"], "dep": []}
        cat = MockCatalog(_graph(deps), deps, satisfied={"dep"})
        order = []
        o = _make(cat, MockDownloader(), MockTab(), MockBrew(), native={}, order=order)
        report = await o.install(["app"])
        assert "dep" not in report.outcomes
        assert order == ["app"]

    async def test_nothing_to_do_when_all_satisfied(self) -> None:
        """Tests that nothing is done when all dependencies are satisfied."""
        deps = {"x": []}
        cat = MockCatalog(_graph(deps), deps, satisfied={"x"})
        o = _make(cat, MockDownloader(), MockTab(), MockBrew(), native={})
        report = await o.install(["x"])
        assert report.outcomes == {}


class TestFallbackPaths:
    """Tests for fallback paths when installation fails."""

    async def test_download_failure_falls_back_to_brew_install(self) -> None:
        """Tests that download failures fall back to brew install."""
        cat = MockCatalog({"x": MockFormula("x")}, {"x": []})
        brew = MockBrew()
        o = _make(cat, FailingDownloader(), MockTab(), brew, native={})
        report = await o.install(["x"])
        assert report.outcomes["x"] is Outcome.BREW_INSTALL
        assert ("install", "x") in brew.calls

    async def test_missing_tab_falls_back_to_brew_install(self) -> None:
        """Tests that missing tabs fall back to brew install."""
        cat = MockCatalog({"x": MockFormula("x")}, {"x": []})
        brew = MockBrew()
        o = _make(cat, MockDownloader(), MockTab(fail={"x"}), brew, native={})
        report = await o.install(["x"])
        assert report.outcomes["x"] is Outcome.BREW_INSTALL

    async def test_install_stage_failure_falls_back_to_brew_install(self) -> None:
        """Tests that install stage failures fall back to brew install."""
        cat = MockCatalog({"x": MockFormula("x")}, {"x": []})
        brew = MockBrew()
        o = _make(
            cat,
            MockDownloader(),
            MockTab(),
            brew,
            native={"x": _NativeResult(stage="install", error="extract boom")},
        )
        report = await o.install(["x"])
        assert report.outcomes["x"] is Outcome.BREW_INSTALL
        assert ("install", "x") in brew.calls

    async def test_link_failure_falls_back_to_brew_link(self) -> None:
        """Tests that link failures fall back to brew link."""
        cat = MockCatalog({"x": MockFormula("x")}, {"x": []})
        brew = MockBrew(link_ok=True)
        o = _make(
            cat,
            MockDownloader(),
            MockTab(),
            brew,
            native={
                "x": _NativeResult(stage="link", dest=Path("/d"), error="conflict")
            },
        )
        report = await o.install(["x"])
        assert report.outcomes["x"] is Outcome.BREW_LINK
        assert ("link", "x") in brew.calls

    async def test_link_failure_then_brew_link_fails_leaves_unlinked(self) -> None:
        """Tests that link failures followed by brew link failures leave the formula unlinked."""
        cat = MockCatalog({"x": MockFormula("x")}, {"x": []})
        brew = MockBrew(link_ok=False)
        o = _make(
            cat,
            MockDownloader(),
            MockTab(),
            brew,
            native={"x": _NativeResult(stage="link", dest=Path("/d"))},
        )
        report = await o.install(["x"])
        assert report.outcomes["x"] is Outcome.INSTALLED_UNLINKED

    async def test_brew_install_failure_marks_failed(self) -> None:
        """Tests that brew install failures are marked as failed."""
        cat = MockCatalog({"x": MockFormula("x")}, {"x": []})
        brew = MockBrew(install_ok=False)
        o = _make(cat, FailingDownloader(), MockTab(), brew, native={})
        report = await o.install(["x"])
        assert report.outcomes["x"] is Outcome.FAILED
        assert "x" in report.failed

    async def test_dependent_skipped_when_dependency_fails(self) -> None:
        """Tests that dependent formulas are skipped when a dependency fails."""
        deps = {"app": ["badlib"], "badlib": []}
        cat = MockCatalog(_graph(deps), deps)
        brew = MockBrew(install_ok=False)  # badlib's brew fallback also fails
        o = _make(
            cat,
            MockDownloader(),
            MockTab(),
            brew,
            native={"badlib": _NativeResult(stage="install", error="boom")},
        )
        report = await o.install(["app"])
        assert report.outcomes["badlib"] is Outcome.FAILED
        assert report.outcomes["app"] is Outcome.SKIPPED_DEP_FAILED


class TestKegOnlyPostInstall:
    """Tests for keg-only formulas and their post-install behavior."""

    async def test_keg_only_reports_native_keg_only(self) -> None:
        """Tests that keg-only formulas are reported as native keg-only."""
        cat = MockCatalog({"ko": MockFormula("ko", keg_only=True)}, {"ko": []})
        o = _make(cat, MockDownloader(), MockTab(), MockBrew(), native={})
        report = await o.install(["ko"])
        assert report.outcomes["ko"] is Outcome.NATIVE_KEG_ONLY

    async def test_post_install_hook_invoked(self) -> None:
        """Tests that post-install hooks are invoked."""
        cat = MockCatalog({"p": MockFormula("p", post_install=True)}, {"p": []})
        brew = MockBrew()
        o = _make(cat, MockDownloader(), MockTab(), brew, native={})
        await o.install(["p"])
        assert ("postinstall", "p") in brew.calls


class TestPartialCleanup:
    """Tests for partial cleanup of installations."""

    @pytest.mark.integration
    async def test_cleanup_partial_removes_keg_and_dangling_opt(self, tmp_path) -> None:
        """Tests that partial cleanup removes the keg and dangling opt."""
        cfg = InstallConfig(prefix=tmp_path, repository=tmp_path, api_path="/api")
        o = Orchestrator(
            catalog=MockCatalog({}, {}),
            downloader=MockDownloader(),
            tab_fetcher=MockTab(),
            brew=MockBrew(),
            config=cfg,
        )
        keg = tmp_path / "Cellar" / "foo" / "1.0"
        (keg / "bin").mkdir(parents=True)
        f = keg / "bin" / "foo"
        f.write_text("x")
        f.chmod(0o444)  # Read-only, like a real keg
        opt = tmp_path / "opt" / "foo"
        opt.parent.mkdir(parents=True)
        opt.symlink_to(Path("..") / "Cellar" / "foo" / "1.0")

        o._cleanup_partial(keg, "foo")
        assert not keg.exists()
        assert not opt.is_symlink()  # Dangling opt removed

    @pytest.mark.integration
    async def test_cleanup_partial_keeps_opt_pointing_at_other_version(
        self, tmp_path
    ) -> None:
        """Tests that partial cleanup keeps the opt symlink pointing at another version."""
        cfg = InstallConfig(prefix=tmp_path, repository=tmp_path, api_path="/api")
        o = Orchestrator(
            catalog=MockCatalog({}, {}),
            downloader=MockDownloader(),
            tab_fetcher=MockTab(),
            brew=MockBrew(),
            config=cfg,
        )

        # opt points at a *different, valid* version (an upgrade scenario)
        other = tmp_path / "Cellar" / "foo" / "0.9"
        other.mkdir(parents=True)
        failed = tmp_path / "Cellar" / "foo" / "1.0"
        failed.mkdir(parents=True)
        opt = tmp_path / "opt" / "foo"
        opt.parent.mkdir(parents=True)
        opt.symlink_to(Path("..") / "Cellar" / "foo" / "0.9")

        o._cleanup_partial(failed, "foo")
        assert not failed.exists()
        assert opt.is_symlink() and opt.exists()  # Still valid -> kept
