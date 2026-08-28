"""Test doubles shared across the unit, integration and CLI layers.

`MockHTTPClient` is used for tests that accept an injected client; for tests
building their own client, `httpx.MockTransport` is used instead so the real
httpx request/response machinery still runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

import httpx

from brewery.core.catalog import api

if TYPE_CHECKING:
    from pathlib import Path


class _NullSink:
    """Stands in for StreamRelocator where the keg is already staged."""

    def finish(self, keg: Path) -> None:
        """Do nothing, as there is nothing staged to relocate.

        Args:
            keg: The keg directory, ignored.
        """


class MockHTTPClient:
    """Async httpx-like stub, either injected into code or patched in for it.

    Construct with either a single canned response/exception, or a mapping of
    `url -> response`; every GET is recorded (url + request headers) so tests
    can assert that conditional validators were sent.

    Args:
        response: One of an `httpx.Response`, an `Exception` to raise, a
            `dict[str, httpx.Response]` keyed by URL, or `None`.
        raise_on_get: If set, every GET raises this exception (used for
            transport-error paths), regardless of `response`.
    """

    def __init__(self, response=None, *, raise_on_get=None) -> None:
        """Initialise a MockHTTPClient.

        Args:
            response: One of an `httpx.Response`, an `Exception` to raise, a
                `dict[str, httpx.Response]` keyed by URL, or `None`.
            raise_on_get: If set, every GET raises this exception (used for
                transport-error paths), regardless of `response`.
        """
        self._map = response if isinstance(response, dict) else None
        self._single = None if isinstance(response, dict) else response
        self._raise_on_get = raise_on_get
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.closed = False
        self.kwargs: dict = {}

    async def __aenter__(self) -> Self:
        """Enter the client's context.

        Returns:
            The mock client instance.
        """
        return self

    async def __aexit__(self, *exc) -> bool:
        """Record that the context owner closed the client.

        Returns:
            False, so no exception is swallowed.
        """
        self.closed = True

        return False

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        follow_redirects: bool = False,
    ) -> httpx.Response | None:
        """Simulate an HTTP GET request.

        Args:
            url: The URL to fetch.
            headers: Headers to include in the request.
            timeout: Request timeout.
            follow_redirects: Whether to follow redirects.

        Returns:
            The canned response for `url`, or None if none was configured.

        Raises:
            AssertionError: If a mapping was given and `url` is not in it.
        """
        self.last_url = url
        self.last_headers = dict(headers or {})
        self.requests.append((url, dict(headers or {})))

        if self._raise_on_get is not None:
            raise self._raise_on_get

        if self._map is not None:
            if url not in self._map:
                raise AssertionError(f"unexpected URL fetched: {url}")

            return self._map[url]

        if isinstance(self._single, Exception):
            raise self._single

        return self._single

    async def aclose(self) -> None:
        """Record that the caller closed the client."""
        self.closed = True


def patch_httpx(monkeypatch) -> MockHTTPClient:
    """Patch httpx.AsyncClient with a stub that records its constructor kwargs.

    The modules under test do a plain `import httpx`, so patching the attribute on
    the shared module object covers whichever entry point is under test.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The MockHTTPClient the patched constructor returns.
    """
    client = MockHTTPClient()

    def _client(**kwargs) -> MockHTTPClient:
        """Record the constructor kwargs and return the stub.

        Args:
            **kwargs: The keyword arguments the service passes to httpx.

        Returns:
            The mock client instance.
        """
        client.kwargs = kwargs

        return client

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    return client


def ok(body: bytes, **headers: str) -> httpx.Response:
    """A 200 response carrying `body` and any validator headers.

    Args:
        body: The response body.
        **headers: Additional headers to include, e.g. `etag`.

    Returns:
        The response.
    """
    return httpx.Response(200, content=body, headers=headers)


def not_modified() -> httpx.Response:
    """A 304, the answer to a conditional request whose validator still matches.

    Returns:
        The response.
    """
    return httpx.Response(304)


def both_feeds(
    formula_resp: httpx.Response, cask_resp: httpx.Response
) -> dict[str, httpx.Response]:
    """Map both API feed URLs to their responses, for a refresh that reads each.

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
