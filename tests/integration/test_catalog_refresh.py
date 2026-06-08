"""Integration tests for the daemon catalog-refresh orchestration.

refresh_catalog accepts an injectable HTTP client, so these tests drive it
with a fake client returning canned responses (200 with a body, or 304). The
real catalog fixture provides get_meta/set_meta, so validator round-tripping and
the load-then-store ordering are exercised end to end against SQLite.
"""

from __future__ import annotations

import httpx
import pytest

from brewery.core import catalog_api
from brewery.daemon.catalog_refresh import refresh_catalog

pytestmark = pytest.mark.integration


class FakeClient:
    """Minimal HTTP client stub serving canned responses keyed by URL.

    Records each GET (url + conditional headers) so tests can assert that
    validators were sent on the request.
    """

    def __init__(self, responses: dict[str, httpx.Response]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, dict]] = []
        self.closed = False

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        self.requests.append((url, dict(headers or {})))
        if url not in self._responses:
            raise AssertionError(f"unexpected URL fetched: {url}")
        return self._responses[url]

    async def aclose(self) -> None:
        self.closed = True


def _empty_catalog(tmp_path):
    from brewery.core.catalog import Catalog

    return Catalog(db_path=tmp_path / "refresh.db")


def _formula_body(fixture_text) -> bytes:
    """Unwrap the fixture into the raw top-level list load_formulae expects.

    The real formula.json is a top-level JSON array; the test fixture wraps it
    as {"formulae": [...]}, so the list is extracted here.
    """
    import orjson

    return orjson.dumps(orjson.loads(fixture_text["formula"])["formulae"])


def _cask_body(fixture_text) -> bytes:
    """Unwrap the fixture into the raw top-level list load_casks expects."""
    import orjson

    return orjson.dumps(orjson.loads(fixture_text["cask"])["casks"])


def _ok(body: bytes, **headers: str) -> httpx.Response:
    return httpx.Response(200, content=body, headers=headers)


def _not_modified() -> httpx.Response:
    return httpx.Response(304)


def _both_feeds(
    formula_resp: httpx.Response, cask_resp: httpx.Response
) -> dict[str, httpx.Response]:
    return {
        catalog_api.FORMULA_FEED.url: formula_resp,
        catalog_api.CASK_FEED.url: cask_resp,
    }


class TestRefreshLoads:
    """Tests for the changed-feed load path."""

    async def test_both_feeds_loaded_into_catalog(self, tmp_path, fixture_text):
        """Test that 200 responses load both feeds into the catalog."""
        cat = _empty_catalog(tmp_path)
        client = FakeClient(
            _both_feeds(
                _ok(_formula_body(fixture_text), ETag='"f1"'),
                _ok(_cask_body(fixture_text), ETag='"c1"'),
            )
        )
        try:
            await refresh_catalog(cat, client=client)
            assert cat.get_formula("yazi") is not None
            assert cat.get_formula("act") is not None
            assert cat.get_cask("iina") is not None
        finally:
            cat.close()

    async def test_validators_persisted_after_load(self, tmp_path, fixture_text):
        """Test that ETag/Last-Modified are stored after a changed feed loads."""
        cat = _empty_catalog(tmp_path)
        client = FakeClient(
            _both_feeds(
                _ok(
                    _formula_body(fixture_text),
                    ETag='"f-etag"',
                    **{"Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
                ),
                _ok(_cask_body(fixture_text), ETag='"c-etag"'),
            )
        )
        try:
            await refresh_catalog(cat, client=client)
            assert cat.get_meta("formula_etag") == '"f-etag"'
            assert (
                cat.get_meta("formula_last_modified") == "Mon, 01 Jan 2024 00:00:00 GMT"
            )
            assert cat.get_meta("cask_etag") == '"c-etag"'
        finally:
            cat.close()

    async def test_fetched_at_stamped(self, tmp_path, fixture_text):
        """Test that a fetch time is stamped for each feed."""
        cat = _empty_catalog(tmp_path)
        client = FakeClient(
            _both_feeds(
                _ok(_formula_body(fixture_text)),
                _ok(_cask_body(fixture_text)),
            )
        )
        try:
            await refresh_catalog(cat, client=client)
            assert cat.get_meta("formula_fetched_at") is not None
            assert cat.get_meta("cask_fetched_at") is not None
        finally:
            cat.close()


class TestConditionalRequests:
    """Tests for the validator-driven conditional-request behaviour."""

    async def test_stored_validators_sent_on_request(self, tmp_path, fixture_text):
        """Test that stored validators are sent as conditional headers next time.

        After a first load stores an ETag, a second refresh must send it as
        If-None-Match so the server can answer 304.
        """
        cat = _empty_catalog(tmp_path)
        first = FakeClient(
            _both_feeds(
                _ok(_formula_body(fixture_text), ETag='"f1"'),
                _ok(_cask_body(fixture_text), ETag='"c1"'),
            )
        )
        try:
            await refresh_catalog(cat, client=first)

            second = FakeClient(_both_feeds(_not_modified(), _not_modified()))
            await refresh_catalog(cat, client=second)

            formula_req = next(
                h for url, h in second.requests if url == catalog_api.FORMULA_FEED.url
            )
            assert formula_req.get("If-None-Match") == '"f1"'
        finally:
            cat.close()

    async def test_not_modified_preserves_data(self, tmp_path, fixture_text):
        """Test that a 304 leaves previously-loaded data intact.

        A refresh that returns 304 for both feeds must not wipe the catalog
        loaded by the prior refresh.
        """
        cat = _empty_catalog(tmp_path)
        first = FakeClient(
            _both_feeds(
                _ok(_formula_body(fixture_text), ETag='"f1"'),
                _ok(_cask_body(fixture_text), ETag='"c1"'),
            )
        )
        try:
            await refresh_catalog(cat, client=first)
            second = FakeClient(_both_feeds(_not_modified(), _not_modified()))
            await refresh_catalog(cat, client=second)
            assert cat.get_formula("yazi") is not None
            assert cat.get_cask("iina") is not None
        finally:
            cat.close()

    async def test_not_modified_still_stamps_fetched_at(self, tmp_path, fixture_text):
        """Test that a 304 still updates the fetch timestamp.

        store_validators runs on both the modified and 304 paths, so the
        fetched_at stamp advances even when nothing was downloaded.
        """
        cat = _empty_catalog(tmp_path)
        first = FakeClient(
            _both_feeds(
                _ok(_formula_body(fixture_text), ETag='"f1"'),
                _ok(_cask_body(fixture_text), ETag='"c1"'),
            )
        )
        try:
            await refresh_catalog(cat, client=first)
            second = FakeClient(_both_feeds(_not_modified(), _not_modified()))
            await refresh_catalog(cat, client=second)
            after = cat.get_meta("formula_fetched_at")
            assert after is not None
            # 304 path preserves the stored ETag rather than clearing it
            assert cat.get_meta("formula_etag") == '"f1"'
        finally:
            cat.close()


class TestErrorHandling:
    """Tests for the failure paths."""

    async def test_http_error_raises_catalog_fetch_error(self, tmp_path):
        """Test that an unexpected HTTP status raises CatalogFetchError."""
        from brewery.core.catalog_api import CatalogFetchError

        cat = _empty_catalog(tmp_path)
        client = FakeClient(_both_feeds(httpx.Response(500), httpx.Response(500)))
        try:
            with pytest.raises(CatalogFetchError):
                await refresh_catalog(cat, client=client)
        finally:
            cat.close()

    async def test_network_error_raises_catalog_fetch_error(self, tmp_path):
        """Test that a transport error is wrapped as CatalogFetchError."""
        from brewery.core.catalog_api import CatalogFetchError

        class BoomClient(FakeClient):
            async def get(
                self,
                url: str,
                *,
                headers: dict[str, str] | None = None,
                timeout: float = 30.0,
                follow_redirects: bool = False,
            ) -> httpx.Response:
                raise httpx.ConnectError("boom")

        cat = _empty_catalog(tmp_path)
        client = BoomClient({})
        try:
            with pytest.raises(CatalogFetchError):
                await refresh_catalog(cat, client=client)
        finally:
            cat.close()

    async def test_failure_does_not_persist_validators(self, tmp_path):
        """Test that a failed fetch leaves no validators behind.

        The error is raised before store_validators runs, so no ETag is written
        for the failing feed.
        """
        from brewery.core.catalog_api import CatalogFetchError

        cat = _empty_catalog(tmp_path)
        client = FakeClient(_both_feeds(httpx.Response(503), httpx.Response(200)))
        try:
            with pytest.raises(CatalogFetchError):
                await refresh_catalog(cat, client=client)
            assert cat.get_meta("formula_etag") is None
        finally:
            cat.close()


class TestClientLifecycle:
    """Tests for client ownership semantics."""

    async def test_injected_client_not_closed(self, tmp_path, fixture_text):
        """Test that a caller-supplied client is not closed by refresh_catalog."""
        cat = _empty_catalog(tmp_path)
        client = FakeClient(
            _both_feeds(
                _ok(_formula_body(fixture_text)),
                _ok(_cask_body(fixture_text)),
            )
        )
        try:
            await refresh_catalog(cat, client=client)
            assert client.closed is False
        finally:
            cat.close()
