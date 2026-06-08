"""Unit tests for catalog_api fetch_feed/fetch_single_formula and validators."""

from __future__ import annotations

import httpx
import pytest

from brewery.core.catalog_api import (
    FORMULA_FEED,
    CatalogFetchError,
    fetch_feed,
    fetch_single_formula,
    read_validators,
    store_validators,
)

pytestmark = pytest.mark.unit


class FakeClient:
    """Minimal HTTP client stub returning a canned response and recording the request."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.last_headers: dict[str, str] | None = None
        self.last_url: str | None = None

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        self.last_url = url
        self.last_headers = dict(headers or {})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeMetaStore:
    """In-memory get_meta/set_meta backing for validator tests."""

    def __init__(self, initial=None):
        self._d = dict(initial or {})

    def get_meta(self, key):
        return self._d.get(key)

    def set_meta(self, key, value):
        self._d[key] = value


class TestFetchFeed:
    """Tests for fetch_feed."""

    async def test_200_returns_modified_with_body_and_validators(self):
        """Test that a 200 yields modified=True with body and new validators."""
        client = FakeClient(
            httpx.Response(
                200,
                content=b"[]",
                headers={"ETag": '"e1"', "Last-Modified": "Mon, 01 Jan 2024"},
            )
        )
        result = await fetch_feed(FORMULA_FEED, client=client)
        assert result.modified is True
        assert result.body == b"[]"
        assert result.etag == '"e1"'
        assert result.last_modified == "Mon, 01 Jan 2024"

    async def test_304_returns_not_modified_echoing_validators(self):
        """Test that a 304 yields modified=False, no body, echoed validators."""
        client = FakeClient(httpx.Response(304))
        result = await fetch_feed(
            FORMULA_FEED, etag='"old"', last_modified="then", client=client
        )
        assert result.modified is False
        assert result.body is None
        assert result.etag == '"old"'
        assert result.last_modified == "then"

    async def test_conditional_headers_sent(self):
        """Test that stored validators become If-None-Match/If-Modified-Since."""
        client = FakeClient(httpx.Response(304))
        await fetch_feed(FORMULA_FEED, etag='"e1"', last_modified="when", client=client)
        if client.last_headers is not None:
            assert client.last_headers["If-None-Match"] == '"e1"'
            assert client.last_headers["If-Modified-Since"] == "when"

    async def test_no_validators_no_conditional_headers(self):
        """Test that absent validators send no conditional headers."""
        client = FakeClient(httpx.Response(200, content=b"[]"))
        await fetch_feed(FORMULA_FEED, client=client)
        if client.last_headers is not None:
            assert "If-None-Match" not in client.last_headers
            assert "If-Modified-Since" not in client.last_headers

    async def test_unexpected_status_raises(self):
        """Test that a non-200/304 status raises CatalogFetchError."""
        client = FakeClient(httpx.Response(500))
        with pytest.raises(CatalogFetchError):
            await fetch_feed(FORMULA_FEED, client=client)

    async def test_network_error_wrapped(self):
        """Test that an httpx error is wrapped as CatalogFetchError."""
        client = FakeClient(httpx.ConnectError("boom"))
        with pytest.raises(CatalogFetchError):
            await fetch_feed(FORMULA_FEED, client=client)

    async def test_missing_response_etag_is_none(self):
        """Test that a 200 without an ETag header yields etag=None."""
        client = FakeClient(httpx.Response(200, content=b"[]"))
        result = await fetch_feed(FORMULA_FEED, client=client)
        assert result.etag is None
        assert result.last_modified is None


class TestFetchSingleFormula:
    """Tests for fetch_single_formula."""

    async def test_200_returns_body(self):
        """Test that a 200 returns the response bytes."""
        client = FakeClient(httpx.Response(200, content=b'{"name":"wget"}'))
        body = await fetch_single_formula("wget", client=client)
        assert body == b'{"name":"wget"}'

    async def test_404_returns_none(self):
        """Test that a 404 returns None rather than raising."""
        client = FakeClient(httpx.Response(404))
        assert await fetch_single_formula("ghost", client=client) is None

    async def test_unexpected_status_raises(self):
        """Test that another error status raises CatalogFetchError."""
        client = FakeClient(httpx.Response(500))
        with pytest.raises(CatalogFetchError):
            await fetch_single_formula("wget", client=client)

    async def test_network_error_wrapped(self):
        """Test that a transport error is wrapped as CatalogFetchError."""
        client = FakeClient(httpx.ConnectError("down"))
        with pytest.raises(CatalogFetchError):
            await fetch_single_formula("wget", client=client)

    async def test_name_is_url_quoted(self):
        """Test that a name with special characters is percent-quoted in the URL.

        '@' and '+' are kept safe; a space must be encoded.
        """
        client = FakeClient(httpx.Response(200, content=b"{}"))
        await fetch_single_formula("foo bar@2", client=client)
        if client.last_url is not None:
            assert "foo%20bar@2" in client.last_url


class TestValidators:
    """Tests for read_validators / store_validators."""

    def test_read_returns_stored_pair(self):
        """Test that read_validators returns the (etag, last_modified) pair."""
        store = FakeMetaStore({"formula_etag": '"e"', "formula_last_modified": "when"})
        assert read_validators(store, FORMULA_FEED) == ('"e"', "when")

    def test_read_missing_is_none_pair(self):
        """Test that absent validators read back as (None, None)."""
        assert read_validators(FakeMetaStore(), FORMULA_FEED) == (None, None)

    def test_store_persists_validators_on_modified(self):
        """Test that a modified result persists ETag, Last-Modified, fetched_at."""
        from brewery.core.catalog_api import FetchResult

        store = FakeMetaStore()
        result = FetchResult(
            feed=FORMULA_FEED,
            modified=True,
            body=b"[]",
            etag='"new"',
            last_modified="now",
        )
        store_validators(store, result)
        assert store.get_meta("formula_etag") == '"new"'
        assert store.get_meta("formula_last_modified") == "now"
        assert store.get_meta("formula_fetched_at") is not None

    def test_store_stamps_fetched_at_on_not_modified(self):
        """Test that a 304 result stamps fetched_at but writes no new validators."""
        from brewery.core.catalog_api import FetchResult

        store = FakeMetaStore()
        result = FetchResult(
            feed=FORMULA_FEED,
            modified=False,
            body=None,
            etag='"echo"',
            last_modified="echo-lm",
        )
        store_validators(store, result)
        assert store.get_meta("formula_fetched_at") is not None
        # Not-modified path must not overwrite the stored validators
        assert store.get_meta("formula_etag") is None
