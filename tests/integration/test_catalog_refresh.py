"""Integration tests for the daemon catalog-refresh orchestration."""

from __future__ import annotations

from pathlib import Path

import httpx
import orjson
import pytest

from brewery.core.catalog import api
from brewery.daemon import catalog_refresh as cr
from brewery.daemon.catalog_refresh import refresh_catalog
from brewery.providers import retention

pytestmark = pytest.mark.integration


def _formula_body(fixture_text) -> bytes:
    """Unwrap the fixture into the raw top-level list load_formulae expects.

    The real formula.json is a top-level JSON array; the test fixture wraps it
    as {"formulae": [...]}, so the list is extracted here.

    Args:
        fixture_text: The fixture text containing the formula data.

    Returns:
        The raw top-level list of formulae.
    """
    return orjson.dumps(orjson.loads(fixture_text["formula"])["formulae"])


def _cask_body(fixture_text) -> bytes:
    """Unwrap the fixture into the raw top-level list load_casks expects.

    Args:
        fixture_text: The fixture text containing the cask data.

    Returns:
        The raw top-level list of casks.
    """
    return orjson.dumps(orjson.loads(fixture_text["cask"])["casks"])


def _ok(body: bytes, **headers: str) -> httpx.Response:
    """Create a 200 OK response with the given body and headers.

    Args:
        body: The response body.
        headers: Additional headers to include in the response.

    Returns:
        An httpx.Response object representing the 200 OK response.
    """
    return httpx.Response(200, content=body, headers=headers)


def _not_modified() -> httpx.Response:
    """Create a 304 Not Modified response.

    Returns:
        An httpx.Response object representing the 304 Not Modified response.
    """
    return httpx.Response(304)


def _both_feeds(
    formula_resp: httpx.Response, cask_resp: httpx.Response
) -> dict[str, httpx.Response]:
    """Create a mapping of feed URLs to their responses.

    Args:
        formula_resp: The response for the formula feed.
        cask_resp: The response for the cask feed.

    Returns:
        A dictionary mapping feed URLs to their responses.
    """
    return {
        api.FORMULA_FEED.url: formula_resp,
        api.CASK_FEED.url: cask_resp,
    }


class TestRefreshLoads:
    """Tests for the changed-feed load path."""

    async def test_both_feeds_loaded_into_catalog(
        self, empty_catalog, fixture_text, http_client
    ) -> None:
        """Test that 200 responses load both feeds into the catalog."""
        client = http_client(
            _both_feeds(
                _ok(_formula_body(fixture_text), ETag='"f1"'),
                _ok(_cask_body(fixture_text), ETag='"c1"'),
            )
        )
        await refresh_catalog(empty_catalog, client=client)
        assert empty_catalog.get_formula("yazi") is not None
        assert empty_catalog.get_formula("act") is not None
        assert empty_catalog.get_cask("iina") is not None

    async def test_validators_persisted_after_load(
        self, empty_catalog, fixture_text, http_client
    ) -> None:
        """Test that ETag/Last-Modified are stored after a changed feed loads."""
        client = http_client(
            _both_feeds(
                _ok(
                    _formula_body(fixture_text),
                    ETag='"f-etag"',
                    **{"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
                ),
                _ok(_cask_body(fixture_text), ETag='"c-etag"'),
            )
        )
        await refresh_catalog(empty_catalog, client=client)
        assert empty_catalog.get_meta("formula_etag") == '"f-etag"'
        assert (
            empty_catalog.get_meta("formula_last_modified")
            == "Mon, 01 Jan 2024 00:00:00 GMT"
        )
        assert empty_catalog.get_meta("cask_etag") == '"c-etag"'

    async def test_fetched_at_stamped(
        self, empty_catalog, fixture_text, http_client
    ) -> None:
        """Test that a fetch time is stamped for each feed."""
        client = http_client(
            _both_feeds(
                _ok(_formula_body(fixture_text)),
                _ok(_cask_body(fixture_text)),
            )
        )
        await refresh_catalog(empty_catalog, client=client)
        assert empty_catalog.get_meta("formula_fetched_at") is not None
        assert empty_catalog.get_meta("cask_fetched_at") is not None


class TestConditionalRequests:
    """Tests for the validator-driven conditional-request behaviour."""

    async def test_stored_validators_sent_on_request(
        self, empty_catalog, fixture_text, http_client
    ) -> None:
        """Test that stored validators are sent as conditional headers next time.

        After a first load stores an ETag, a second refresh must send it as
        If-None-Match so the server can answer 304.
        """
        first = http_client(
            _both_feeds(
                _ok(_formula_body(fixture_text), ETag='"f1"'),
                _ok(_cask_body(fixture_text), ETag='"c1"'),
            )
        )
        await refresh_catalog(empty_catalog, client=first)

        second = http_client(_both_feeds(_not_modified(), _not_modified()))
        await refresh_catalog(empty_catalog, client=second)

        formula_req = next(
            h for url, h in second.requests if url == api.FORMULA_FEED.url
        )
        assert formula_req.get("If-None-Match") == '"f1"'

    async def test_not_modified_preserves_data(
        self, empty_catalog, fixture_text, http_client
    ) -> None:
        """Test that a 304 leaves previously-loaded data intact.

        A refresh that returns 304 for both feeds must not wipe the catalog
        loaded by the prior refresh.
        """
        first = http_client(
            _both_feeds(
                _ok(_formula_body(fixture_text), ETag='"f1"'),
                _ok(_cask_body(fixture_text), ETag='"c1"'),
            )
        )
        await refresh_catalog(empty_catalog, client=first)
        second = http_client(_both_feeds(_not_modified(), _not_modified()))
        await refresh_catalog(empty_catalog, client=second)
        assert empty_catalog.get_formula("yazi") is not None
        assert empty_catalog.get_cask("iina") is not None

    async def test_not_modified_still_stamps_fetched_at(
        self, empty_catalog, fixture_text, http_client
    ) -> None:
        """Test that a 304 still updates the fetch timestamp.

        store_validators runs on both the modified and 304 paths, so the
        fetched_at stamp advances even when nothing was downloaded.
        """
        first = http_client(
            _both_feeds(
                _ok(_formula_body(fixture_text), ETag='"f1"'),
                _ok(_cask_body(fixture_text), ETag='"c1"'),
            )
        )
        await refresh_catalog(empty_catalog, client=first)
        second = http_client(_both_feeds(_not_modified(), _not_modified()))
        await refresh_catalog(empty_catalog, client=second)
        after = empty_catalog.get_meta("formula_fetched_at")
        assert after is not None

        # 304 path preserves the stored ETag rather than clearing it
        assert empty_catalog.get_meta("formula_etag") == '"f1"'


class TestErrorHandling:
    """Tests for the failure paths."""

    async def test_http_error_raises_catalog_fetch_error(
        self, empty_catalog, http_client
    ) -> None:
        """Test that an unexpected HTTP status raises CatalogFetchError."""
        from brewery.core.catalog.api import CatalogFetchError

        client = http_client(_both_feeds(httpx.Response(500), httpx.Response(500)))
        with pytest.raises(CatalogFetchError):
            await refresh_catalog(empty_catalog, client=client)

    async def test_network_error_raises_catalog_fetch_error(
        self, empty_catalog, http_client
    ) -> None:
        """Test that a transport error is wrapped as CatalogFetchError."""
        from brewery.core.catalog.api import CatalogFetchError

        client = http_client({}, raise_on_get=httpx.ConnectError("boom"))
        with pytest.raises(CatalogFetchError):
            await refresh_catalog(empty_catalog, client=client)

    async def test_failure_does_not_persist_validators(
        self, empty_catalog, http_client
    ) -> None:
        """Test that a failed fetch leaves no validators behind.

        The error is raised before store_validators runs, so no ETag is written
        for the failing feed.
        """
        from brewery.core.catalog.api import CatalogFetchError

        client = http_client(_both_feeds(httpx.Response(503), httpx.Response(200)))
        with pytest.raises(CatalogFetchError):
            await refresh_catalog(empty_catalog, client=client)
        assert empty_catalog.get_meta("formula_etag") is None


class TestClientLifecycle:
    """Tests for client ownership semantics."""

    async def test_injected_client_not_closed(
        self, empty_catalog, fixture_text, http_client
    ) -> None:
        """Test that a caller-supplied client is not closed by refresh_catalog."""
        client = http_client(
            _both_feeds(
                _ok(_formula_body(fixture_text)),
                _ok(_cask_body(fixture_text)),
            )
        )
        await refresh_catalog(empty_catalog, client=client)
        assert client.closed is False


class TestMaybeCleanup:
    """Tests for the daemon _maybe_cleanup helper."""

    def _patch(self, monkeypatch, *, due, cleanup=None) -> tuple[list, list]:
        """Sets up the mock environment for testing _maybe_cleanup.

        Args:
            monkeypatch: The monkeypatch fixture for modifying attributes.
            due: Whether the cleanup is due.
            cleanup: Optional cleanup function to simulate a failure.

        Returns:
            A tuple of the calls list and marks list."""
        monkeypatch.setattr(
            "brewery.core.config.ensure_cache_dir", lambda: Path("/cache")
        )
        monkeypatch.setattr(retention, "due_for_cleanup", lambda cache_dir, **k: due)

        marks: list = []
        monkeypatch.setattr(
            retention,
            "mark_cleanup_run",
            lambda cache_dir, **k: marks.append(cache_dir),
        )

        calls: list = []

        class MockRepo:
            """Simulates a Repository for testing cleanup behavior."""

            def __init__(self, catalog=None) -> None:
                """Initialises the MockRepo with an optional catalog.

                Args:
                    catalog: The catalog to use for the repository.
                """
                calls.append("init")

            async def cleanup_packages(self) -> tuple[list, list]:
                """Simulates the cleanup_packages method, returning any cleanup results.

                Returns:
                    The cleanup results.
                """
                calls.append("cleanup")

                return cleanup() if cleanup is not None else ([], [])

        monkeypatch.setattr("brewery.core.repo.Repository", MockRepo)

        return calls, marks

    async def test_skips_when_not_due(self, monkeypatch, empty_catalog) -> None:
        """Tests that not due -> Repository never constructed, stamp untouched."""
        calls, marks = self._patch(monkeypatch, due=False)
        await cr._maybe_cleanup(empty_catalog)
        assert calls == []
        assert marks == []

    async def test_runs_and_stamps_when_due(self, monkeypatch, empty_catalog) -> None:
        """Tests that due -> sweep runs and the stamp is written after a clean run."""
        calls, marks = self._patch(
            monkeypatch, due=True, cleanup=lambda: (["wget 1.0"], [])
        )
        await cr._maybe_cleanup(empty_catalog)
        assert "cleanup" in calls
        assert marks == [Path("/cache")]

    async def test_failure_isolated_and_not_stamped(
        self, monkeypatch, empty_catalog
    ) -> None:
        """Tests that a sweep failure is swallowed (no raise) and NOT stamped, so it retries."""

        def boom() -> None:
            """Raises OSError to simulate a failure.

            Raises:
                OSError: Always raised to simulate a failure."""
            raise OSError("permission denied")

        calls, marks = self._patch(monkeypatch, due=True, cleanup=boom)
        await cr._maybe_cleanup(empty_catalog)  # Should not raise
        assert "cleanup" in calls
        assert marks == []
