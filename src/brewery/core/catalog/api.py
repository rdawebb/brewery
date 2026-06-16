"""Conditional fetcher for the Homebrew catalog feeds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import quote

import httpx

from brewery.core.errors import CatalogFetchError
from brewery.core.logging import BreweryLogger, get_logger

log: BreweryLogger = get_logger(name=__name__)

_USER_AGENT = "brewery/0.1.0"
_DEFAULT_TIMEOUT = 30.0
_HTTP_OK = 200
_HTTP_NOT_MODIFIED = 304
_HTTP_NOT_FOUND = 404

_SINGLE_FORMULA_URL = "https://formulae.brew.sh/api/formula/{name}.json"


class _HttpClient(Protocol):
    """Structural protocol for an async HTTP client that can perform GET requests."""

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = ...,
        timeout: float = ...,
        follow_redirects: bool = ...,
    ) -> httpx.Response: ...


@dataclass(frozen=True, slots=True)
class CatalogFeed:
    """One upstream catalog feed and the meta keys its validators live under."""

    name: str  # "formula" | "cask"
    url: str
    etag_key: str
    last_modified_key: str

    @property
    def fetched_at_key(self) -> str:
        """Meta key recording the last successful fetch/check time.

        Returns:
            The meta key name for the last successful fetch time.
        """
        return f"{self.name}_fetched_at"


FORMULA_FEED = CatalogFeed(
    name="formula",
    url="https://formulae.brew.sh/api/formula.json",
    etag_key="formula_etag",
    last_modified_key="formula_last_modified",
)
CASK_FEED = CatalogFeed(
    name="cask",
    url="https://formulae.brew.sh/api/cask.json",
    etag_key="cask_etag",
    last_modified_key="cask_last_modified",
)
FEEDS: tuple[CatalogFeed, ...] = (FORMULA_FEED, CASK_FEED)


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of a conditional feed fetch.

    When `modified` is False the server returned 304 and `body` is None; the
    stored validators are echoed back unchanged. When True, `body` holds the
    decompressed feed bytes and `etag`/`last_modified` are the new validators
    to persist after the body is written.
    """

    feed: CatalogFeed
    modified: bool
    body: bytes | None
    etag: str | None
    last_modified: str | None


async def fetch_feed(
    feed: CatalogFeed,
    etag: str | None = None,
    last_modified: str | None = None,
    *,
    client: _HttpClient | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> FetchResult:
    """Conditionally fetch a catalog feed.

    Fetches both feeds concurrently by gathering with a shared client.

    Args:
        feed: The feed to fetch.
        etag: Previously stored ETag, sent as `If-None-Match`.
        last_modified: Previously stored Last-Modified, sent as `If-Modified-Since`.
        client: Existing `httpx.AsyncClient` to reuse, or `None` to create a new one.
        timeout: Socket timeout in seconds.

    Returns:
        `modified=False` on a 304, otherwise the body and new validators.

    Raises:
        CatalogFetchError: On a network error or an unexpected HTTP status.
    """
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag

    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        response: httpx.Response = await _get(
            url=feed.url, headers=headers, client=client, timeout=timeout
        )

    except httpx.HTTPError as e:
        raise CatalogFetchError(
            message=f"Catalog feed '{feed.name}' could not be reached",
            context={
                "feed": feed.name,
                "url": feed.url,
                "error": str(object=e),
            },
        ) from e

    if response.status_code == _HTTP_NOT_MODIFIED:
        log.info(event="catalog_feed_not_modified", feed=feed.name)
        return FetchResult(
            feed=feed,
            modified=False,
            body=None,
            etag=etag,
            last_modified=last_modified,
        )

    if response.status_code != _HTTP_OK:
        raise CatalogFetchError(
            message=f"Catalog feed '{feed.name}' returned HTTP {response.status_code}",
            context={
                "feed": feed.name,
                "url": feed.url,
                "status": response.status_code,
            },
        )

    body: bytes = response.content
    log.info(
        event="catalog_feed_fetched",
        feed=feed.name,
        bytes=len(body),
        etag=response.headers.get("ETag"),
    )

    return FetchResult(
        feed=feed,
        modified=True,
        body=body,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
    )


async def fetch_single_formula(
    name: str,
    *,
    client: _HttpClient | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bytes | None:
    """Fetch one formula's JSON from the per-name endpoint (fallback for info lookups)

    Args:
        name: Formula name
        client: Existing `httpx.AsyncClient` to reuse, or `None` to create a new one.
        timeout: Socket timeout in seconds.

    Returns:
        The decompressed JSON bytes, or None if the formula does not exist (404).

    Raises:
        CatalogFetchError: On a network error or an unexpected HTTP status.
    """
    url: str = _SINGLE_FORMULA_URL.format(name=quote(string=name, safe="@+"))
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}

    try:
        response: httpx.Response = await _get(
            url=url, headers=headers, client=client, timeout=timeout
        )

    except httpx.HTTPError as e:
        raise CatalogFetchError(
            message=f"Single-formula fetch for '{name}' could not be reached",
            context={
                "name": name,
                "url": url,
                "error": str(object=e),
            },
        ) from e

    if response.status_code == _HTTP_NOT_FOUND:
        return None

    if response.status_code != _HTTP_OK:
        raise CatalogFetchError(
            message=f"Single-formula fetch for '{name}' returned HTTP {response.status_code}",
            context={"name": name, "url": url, "status": response.status_code},
        )

    return response.content


def read_validators(
    catalog: object, feed: CatalogFeed
) -> tuple[str | None, str | None]:
    """Read the stored ETag and Last-Modified for a feed from the catalog meta.

    Args:
        catalog: A Catalog-like object exposing `get_meta(key)`.
        feed: The feed whose validators to read.

    Returns:
        (etag, last_modified) tuple, either element may be None.
    """
    get_meta = catalog.get_meta  # ty: ignore[unresolved-attribute]

    return get_meta(feed.etag_key), get_meta(feed.last_modified_key)


def store_validators(catalog: object, result: FetchResult) -> None:
    """Persist a fetch's validators and stamp the fetch time.

    Args:
        catalog: A Catalog-like object exposing `set_meta(key, value)`.
        result: The fetch result whose validators to persist.
    """
    set_meta = catalog.set_meta  # ty: ignore[unresolved-attribute]
    if result.modified:
        if result.etag:
            set_meta(result.feed.etag_key, result.etag)
        if result.last_modified:
            set_meta(result.feed.last_modified_key, result.last_modified)

    set_meta(result.feed.fetched_at_key, _now_iso())


async def _get(
    url: str,
    headers: dict[str, str],
    client: _HttpClient | None,
    timeout: float,
) -> httpx.Response:
    """GET a URL via the supplied client, or an ephemeral one if none given.

    Args:
        url: The URL to GET.
        headers: The request headers.
        client: Existing `httpx.AsyncClient` to reuse, or `None` to create a new one.
        timeout: The request timeout in seconds.

    Returns:
        The response object.
    """
    if client is not None:
        return await client.get(
            url, headers=headers, timeout=timeout, follow_redirects=True
        )

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as ephemeral:
        return await ephemeral.get(url, headers=headers)


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string.

    Returns:
        The current UTC time as an ISO-8601 string.
    """
    return datetime.now(tz=timezone.utc).isoformat()
