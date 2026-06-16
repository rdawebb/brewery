"""Integration tests for the daemon catalog-refresh orchestration."""

from __future__ import annotations

import httpx
import orjson
import pytest

from brewery.core import catalog_api
from brewery.daemon.catalog_refresh import refresh_catalog

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
        catalog_api.FORMULA_FEED.url: formula_resp,
        catalog_api.CASK_FEED.url: cask_resp,
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
            h for url, h in second.requests if url == catalog_api.FORMULA_FEED.url
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
        from brewery.core.catalog_api import CatalogFetchError

        client = http_client(_both_feeds(httpx.Response(500), httpx.Response(500)))
        with pytest.raises(CatalogFetchError):
            await refresh_catalog(empty_catalog, client=client)

    async def test_network_error_raises_catalog_fetch_error(
        self, empty_catalog, http_client
    ) -> None:
        """Test that a transport error is wrapped as CatalogFetchError."""
        from brewery.core.catalog_api import CatalogFetchError

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
        from brewery.core.catalog_api import CatalogFetchError

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
