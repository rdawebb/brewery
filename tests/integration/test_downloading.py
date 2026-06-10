"""Integration tests for the bottle downloader.

All network I/O is mocked with `httpx.MockTransport`; no real requests are
made. Coverage spans the ghcr redirect/auth-scoping, hash verification and
atomic-write cleanup, cache hits, retry/backoff on transient failures, a
mid-stream transport error (so a partial write can't corrupt the cache or the
next attempt), the progress callback, and `fetch_all` concurrency bounding.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

import brewery.providers.downloader as d
from brewery.providers.downloader import BottleRef, Downloader, DownloadError

pytestmark = pytest.mark.integration

CDN = "https://pkg-containers.githubusercontent.com/blob/obj"


def _blob(seed: int, size: int = 4096) -> bytes:
    """Generate a deterministic blob of bytes for testing.

    Args:
        seed: The seed value to base the blob on.
        size: The size of the blob in bytes.

    Returns:
        A bytes object containing the generated blob.
    """
    return bytes((seed + i) % 256 for i in range(size))


def _ref(content: bytes, *, name: str = "foo", host: str = "ghcr.io") -> BottleRef:
    """Create a BottleRef for the given content.

    Args:
        content: The content of the bottle.
        name: The name of the bottle.
        host: The host of the bottle.

    Returns:
        A BottleRef object representing the bottle.
    """
    sha = hashlib.sha256(content).hexdigest()
    url = f"https://{host}/v2/homebrew/core/{name}/blobs/sha256:{sha}"

    return BottleRef(name, url, sha)


@contextlib.asynccontextmanager
async def _make(cache: Path, handler, **kw):
    """Create a Downloader instance for testing.

    Args:
        cache: The path to the cache directory.
        handler: The request handler to use for mocking HTTP requests.
        **kw: Additional keyword arguments to pass to the Downloader.

    Yields:
        A Downloader instance.
    """
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        yield Downloader(cache, client, **kw)


def _redirecting_handler(blob: bytes, *, requests: list | None = None):
    """ghcr blob GET -> 307 to the CDN, which serves the content.

    Args:
        blob: The blob of bytes to serve.
        requests: A list to track the requests made.

    Returns:
        A handler function for the HTTP request.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append((req.url.host, "authorization" in req.headers))
        if req.url.host == "ghcr.io":
            return httpx.Response(307, headers={"Location": CDN})
        return httpx.Response(
            200, content=blob, headers={"Content-Length": str(len(blob))}
        )

    return handler


class _RaisingStream(httpx.AsyncByteStream):
    """Yields some bytes, then raises — simulates a dropped connection."""

    def __init__(self, head: bytes, exc: Exception) -> None:
        """Initialize the raising stream with a head and an exception.

        Args:
            head: The initial bytes to yield.
            exc: The exception to raise after yielding.
        """
        self._head = head
        self._exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the initial bytes, then raise the exception.

        Returns:
            An async iterator yielding the initial bytes.
        """
        yield self._head
        raise self._exc

    async def aclose(self) -> None:
        """Close the stream."""
        pass


class _LiveCounter:
    """Tracks the number of concurrent downloads."""

    def __init__(self) -> None:
        """Initialise the live counter."""
        self.now = 0
        self.peak = 0

    def enter(self) -> None:
        """Mark the start of a download."""
        self.now += 1
        self.peak = max(self.peak, self.now)

    def exit(self) -> None:
        """Mark the end of a download."""
        self.now -= 1


class _SlowStream(httpx.AsyncByteStream):
    """Streams content with a pause so concurrent downloads actually overlap,
    recording how many are in flight at once."""

    def __init__(self, data: bytes, live: _LiveCounter, hold: float = 0.02) -> None:
        """Initialise the slow stream with data, live counter, and hold time.

        Args:
            data: The data to stream.
            live: The live counter to update.
            hold: The time to hold between chunks.
        """
        self._data = data
        self._live = live
        self._hold = hold

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield chunks of data with a delay.

        Yields:
            Chunks of data from the stream.
        """
        self._live.enter()
        try:
            mid = len(self._data) // 2
            yield self._data[:mid]
            await asyncio.sleep(self._hold)
            yield self._data[mid:]

        finally:
            self._live.exit()

    async def aclose(self) -> None:
        """Close the stream."""
        pass


class _PlainStream(httpx.AsyncByteStream):
    """Streams content in one chunk with no known length (no Content-Length)."""

    def __init__(self, data: bytes) -> None:
        """Initialise the plain stream with data.

        Args:
            data: The data to stream.
        """
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the data in one chunk.

        Yields:
            The data to stream.
        """
        yield self._data

    async def aclose(self) -> None:
        """Close the stream."""
        pass


@pytest.fixture
def no_backoff(monkeypatch):
    """Make retry backoff instantaneous so retry tests don't actually sleep."""

    async def _instant(_seconds) -> None:
        """Instantly return without sleeping.

        Args:
            _seconds: The number of seconds to sleep (ignored).
        """
        return None

    monkeypatch.setattr(d.asyncio, "sleep", _instant)


async def test_fetch_downloads_and_verifies(tmp_path) -> None:
    """Test fetching, downloading, and verifying a blob."""
    blob = _blob(1)
    ref = _ref(blob)
    async with _make(tmp_path, _redirecting_handler(blob)) as dl:
        path = await dl.fetch(ref)
    assert path == dl.cache_path(ref.sha256)
    assert path.read_bytes() == blob


async def test_auth_sent_to_ghcr_but_not_cdn(tmp_path) -> None:
    """Test that authentication is sent to GHCR but not to the CDN."""
    blob = _blob(2)
    reqs: list = []
    async with _make(tmp_path, _redirecting_handler(blob, requests=reqs)) as dl:
        await dl.fetch(_ref(blob))
    assert reqs == [
        ("ghcr.io", True),
        ("pkg-containers.githubusercontent.com", False),
    ]


async def test_no_auth_for_non_ghcr_host(tmp_path) -> None:
    """Test that no authentication is sent for non-GHCR hosts."""
    blob = _blob(3)
    reqs: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle HTTP requests.

        Args:
            req: The HTTP request to handle.

        Returns:
            The HTTP response with the requested content.
        """
        reqs.append((req.url.host, "authorization" in req.headers))
        return httpx.Response(200, content=blob)

    ref = _ref(blob, host="example.org")
    async with _make(tmp_path, handler) as dl:
        await dl.fetch(ref)
    assert reqs == [("example.org", False)]


async def test_cache_hit_makes_no_request(tmp_path) -> None:
    """Test that a cache hit does not trigger a network request."""
    blob = _blob(4)
    reqs: list = []
    ref = _ref(blob)
    async with _make(tmp_path, _redirecting_handler(blob, requests=reqs)) as dl:
        await dl.fetch(ref)
        n = len(reqs)
        again = await dl.fetch(ref)
    assert len(reqs) == n  # Second fetch served from cache
    assert again == dl.cache_path(ref.sha256)


async def test_verify_cached_redownloads_corrupt_entry(tmp_path) -> None:
    """Test that verifying a cached entry re-downloads it if corrupt."""
    blob = _blob(5)
    ref = _ref(blob)

    # Pre-seed a corrupt cache entry at the content-addressed path
    corrupt = tmp_path / ref.sha256
    corrupt.write_bytes(b"not the bottle")

    reqs: list = []
    async with _make(
        tmp_path, _redirecting_handler(blob, requests=reqs), verify_cached=True
    ) as dl:
        path = await dl.fetch(ref)
    assert reqs, "corrupt entry should have triggered a re-download"
    assert path.read_bytes() == blob


async def test_sha_mismatch_raises_and_leaves_no_artifact(tmp_path) -> None:
    """Test that a SHA mismatch raises an error and leaves no artifact."""
    blob = _blob(6)
    bad = BottleRef("bar", _ref(blob).url, "0" * 64)  # Wrong expected digest
    async with _make(tmp_path, _redirecting_handler(blob)) as dl:
        with pytest.raises(DownloadError, match="mismatch"):
            await dl.fetch(bad)
    assert not dl.cache_path(bad.sha256).exists()
    assert list(tmp_path.glob("*.part")) == []  # Temp cleaned up


async def test_http_404_raises_without_retry(tmp_path) -> None:
    """Test that a 404 error raises an error without retrying."""
    calls: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle HTTP requests.

        Args:
            req: The HTTP request to handle.

        Returns:
            The HTTP response with the requested content.
        """
        calls.append(req.url.host)
        return httpx.Response(404)

    ref = _ref(_blob(7), host="example.org")
    async with _make(tmp_path, handler, max_retries=3) as dl:
        with pytest.raises(DownloadError, match="HTTP 404"):
            await dl.fetch(ref)
    assert len(calls) == 1  # 4xx is not retried


async def test_retries_then_succeeds_on_transient_5xx(tmp_path, no_backoff) -> None:
    """Test that retries succeed on transient 5xx errors."""
    blob = _blob(8)
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle HTTP requests.

        Args:
            req: The HTTP request to handle.

        Returns:
            The HTTP response with the requested content.
        """
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=blob)

    ref = _ref(blob, host="example.org")
    async with _make(tmp_path, handler, max_retries=3) as dl:
        path = await dl.fetch(ref)
    assert state["n"] == 2
    assert path.read_bytes() == blob


async def test_exhausts_retries_then_raises(tmp_path, no_backoff) -> None:
    """Test that exhausting all retries raises an error."""
    calls: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle HTTP requests.

        Args:
            req: The HTTP request to handle.

        Returns:
            The HTTP response with the requested content.
        """
        calls.append(1)
        return httpx.Response(503)

    ref = _ref(_blob(9), host="example.org")
    async with _make(tmp_path, handler, max_retries=3) as dl:
        with pytest.raises(DownloadError, match="after 3 attempts"):
            await dl.fetch(ref)
    assert len(calls) == 3
    assert list(tmp_path.glob("*.part")) == []


async def test_resumes_cleanly_after_midstream_drop(tmp_path, no_backoff) -> None:
    """Test that a download can resume cleanly after a mid-stream drop."""
    blob = _blob(10)
    ref = _ref(blob, host="example.org")
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle HTTP requests.

        Args:
            req: The HTTP request to handle.

        Returns:
            The HTTP response with the requested content.
        """
        state["n"] += 1
        if state["n"] == 1:
            # Deliver half the bytes, then drop the connection
            return httpx.Response(
                200,
                stream=_RaisingStream(blob[: len(blob) // 2], httpx.ReadError("drop")),
            )
        return httpx.Response(200, content=blob)

    async with _make(tmp_path, handler, max_retries=3) as dl:
        path = await dl.fetch(ref)

    # The retry must not append to the first attempt's partial bytes
    assert path.read_bytes() == blob
    assert list(tmp_path.glob("*.part")) == []


async def test_progress_reports_total_and_bytes(tmp_path) -> None:
    """Test that progress reports the total and bytes downloaded."""
    blob = _blob(11, size=8192)
    ref = _ref(blob)
    seen: list[tuple[int, int | None]] = []
    async with _make(tmp_path, _redirecting_handler(blob)) as dl:
        await dl.fetch(ref, on_progress=lambda done, total: seen.append((done, total)))
    assert seen[-1][0] == len(blob)
    assert all(total == len(blob) for _, total in seen)


async def test_progress_total_none_without_content_length(tmp_path) -> None:
    """Test that progress reports None for total without Content-Length."""
    blob = _blob(12)
    ref = _ref(blob, host="example.org")

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle HTTP requests.

        Args:
            req: The HTTP request to handle.

        Returns:
            The HTTP response with the requested content.
        """
        return httpx.Response(200, stream=_PlainStream(blob))  # No Content-Length

    seen: list[tuple[int, int | None]] = []
    async with _make(tmp_path, handler) as dl:
        await dl.fetch(ref, on_progress=lambda done, total: seen.append((done, total)))
    assert seen and all(total is None for _, total in seen)


async def test_fetch_all_returns_name_to_path_mapping(tmp_path) -> None:
    """Test that fetch_all returns a mapping from names to paths."""
    blobs = {f"f{i}": _blob(20 + i) for i in range(4)}
    refs = [_ref(b, name=n) for n, b in blobs.items()]
    by_sha = {hashlib.sha256(b).hexdigest(): b for b in blobs.values()}

    def handler(req: httpx.Request) -> httpx.Response:
        """Redirect requests to the CDN.

        Args:
            req: The HTTP request to redirect.

        Returns:
            The HTTP response with the redirect location.
        """
        if req.url.host == "ghcr.io":
            return httpx.Response(307, headers={"Location": CDN + "?" + req.url.path})
        sha = req.url.params.get("") or req.url.query.decode()

        # Map back to the blob via the sha embedded in the original ghcr path
        sha = sha.rsplit("sha256:", 1)[-1]
        return httpx.Response(200, content=by_sha[sha])

    async with _make(tmp_path, handler) as dl:
        result = await dl.fetch_all(refs)
    assert set(result) == set(blobs)
    for ref in refs:
        assert result[ref.name] == dl.cache_path(ref.sha256)


async def test_fetch_all_bounds_concurrency(tmp_path) -> None:
    """Test that fetch_all respects the max_concurrency limit."""
    live = _LiveCounter()
    blobs = [_blob(40 + i, size=2048) for i in range(6)]
    by_sha = {hashlib.sha256(b).hexdigest(): b for b in blobs}
    refs = [_ref(b, name=f"g{i}", host="example.org") for i, b in enumerate(blobs)]

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle HTTP requests.

        Args:
            req: The HTTP request to handle.

        Returns:
            The HTTP response with the requested content.
        """
        sha = req.url.path.rsplit("sha256:", 1)[-1]
        return httpx.Response(200, stream=_SlowStream(by_sha[sha], live))

    async with _make(tmp_path, handler, max_concurrency=2) as dl:
        await dl.fetch_all(refs)
    assert live.peak <= 2, f"in-flight peaked at {live.peak}, limit was 2"


async def test_fetch_all_fails_fast_on_bad_ref(tmp_path, no_backoff) -> None:
    """Test that fetch_all fails fast on a bad reference."""
    good = _blob(60)
    bad_url = _ref(_blob(61), name="bad", host="example.org").url

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle HTTP requests.

        Args:
            req: The HTTP request to handle.

        Returns:
            The HTTP response with the requested content.
        """
        if "bad" in req.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=good)

    refs = [
        _ref(good, name="ok", host="example.org"),
        BottleRef("bad", bad_url, "1" * 64),
    ]
    async with _make(tmp_path, handler, max_retries=1) as dl:
        with pytest.raises(DownloadError):
            await dl.fetch_all(refs)
