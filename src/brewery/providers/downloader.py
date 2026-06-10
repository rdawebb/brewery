"""Download Homebrew bottle tarballs from ghcr.io into a content-addressed cache.

Bottle URLs and their expected SHA256 are already resolved and stored in SQLite
(OS-matched), so this module's job is narrow: given a (url, sha256), stream the
blob to a cache keyed by its digest, verifying integrity as it goes.

ghcr.io specifics:
  * Anonymous pulls use Homebrew's hardcoded bearer token `QQ==`. It is sent
    only to ghcr.io — the registry answers a blob GET with a 307 to a presigned
    CDN URL, and forwarding the bearer to that host is both unnecessary and, for
    some object stores, an error. httpx strips `Authorization` on cross-host
    redirects, which is exactly the behaviour we want.

Integrity & atomicity:
  * The cache is content-addressed (`<cache>/<sha256>`). We only ever rename a
    fully-downloaded, hash-verified file into place, so a present cache entry is
    a trusted one and a hit costs no I/O. A failed download leaves no artifact.

Concurrency:
  * `fetch` is the unit of work; `fetch_all` runs a closure of bottles with
    bounded parallelism. The install pipeline can instead call `fetch` per
    formula inside its own try/except to get per-formula fallback to brew.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from brewery.core.errors import BrewError

# Homebrew's hardcoded anonymous bearer for pulling bottles from ghcr.io
DEFAULT_GHCR_TOKEN = "QQ=="
_GHCR_HOSTS = frozenset({"ghcr.io"})
_OCI_LAYER_ACCEPT = "application/vnd.oci.image.layer.v1.tar+gzip"
_CHUNK = 1 << 20  # 1 MiB
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# (downloaded_bytes, total_bytes_or_None)
ProgressCb = Callable[[int, "int | None"], None]


class DownloadError(BrewError):
    """A bottle could not be downloaded or failed verification.

    The pipeline should treat this as a per-formula fallback signal.
    """

    def __init__(self, ref: "BottleRef", reason: str) -> None:
        """Initialise DownloadError.

        Args:
            ref: The reference to the bottle that failed to download.
            reason: The reason for the download failure.
        """
        self.ref = ref
        self.reason = reason
        super().__init__(f"{ref.name} <{ref.url}>: {reason}")


class _Transient(Exception):
    """Internal: a retryable HTTP status was returned."""

    def __init__(self, status: int) -> None:
        """Initialise _Transient.

        Args:
            status: The HTTP status code that triggered the retry.
        """
        self.status = status
        super().__init__(f"HTTP {status}")


@dataclass(frozen=True)
class BottleRef:
    """Reference to a Homebrew bottle.

    Attributes:
        name: The name of the bottle.
        url: The download URL of the bottle.
        sha256: The expected SHA256 checksum of the bottle.
    """

    name: str
    url: str
    sha256: str  # Lowercase hex


def _sha256_file(path: Path) -> str:
    """Calculate the SHA256 checksum of a file.

    Args:
        path: The path to the file.

    Returns:
        The lowercase hex digest of the file's SHA256 checksum.
    """
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)

    return h.hexdigest()


class Downloader:
    def __init__(
        self,
        cache_dir: Path,
        client: httpx.AsyncClient | None = None,
        *,
        token: str = DEFAULT_GHCR_TOKEN,
        max_concurrency: int = 4,
        max_retries: int = 3,
        verify_cached: bool = False,
    ) -> None:
        """
        Initialise the downloader.

        Args:
            cache_dir: The directory to use for caching downloaded bottles.
            client: The HTTP client to use for downloading bottles.
            token: The GitHub Container Registry token to use for authentication.
            max_concurrency: The maximum number of concurrent downloads.
            max_retries: The maximum number of retry attempts for failed downloads.
        """
        self._cache_dir = Path(cache_dir)
        self._client = client
        self._token = token
        self._sem = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._verify_cached = verify_cached

    def cache_path(self, sha256: str) -> Path:
        """Return the cache path for a bottle with the given SHA256 checksum.

        Args:
            sha256: The SHA256 checksum of the bottle.

        Returns:
            The cache path for the bottle.
        """
        return self._cache_dir / sha256

    def _headers(self, url: str) -> dict[str, str]:
        """Return the headers to use for a request to the given URL.

        Args:
            url: The URL to request.

        Returns:
            The headers to include in the request.
        """
        headers = {"Accept": _OCI_LAYER_ACCEPT}
        if self._token and urlsplit(url).hostname in _GHCR_HOSTS:
            headers["Authorization"] = f"Bearer {self._token}"

        return headers

    async def fetch(
        self, ref: BottleRef, *, on_progress: ProgressCb | None = None
    ) -> Path:
        """Return the cached path for `ref`, downloading if necessary.

        Args:
            ref: The bottle reference to fetch.
            on_progress: A callback function to report download progress.

        Returns:
            The path to the cached bottle.
        """
        dest = self.cache_path(ref.sha256)
        if dest.exists():
            if not self._verify_cached or _sha256_file(dest) == ref.sha256:
                return dest

            dest.unlink()  # Corrupt entry; re-download

        self._cache_dir.mkdir(parents=True, exist_ok=True)
        async with self._sem:
            return await self._download(ref, dest, on_progress)

    async def fetch_all(
        self, refs: Iterable[BottleRef], *, on_progress: ProgressCb | None = None
    ) -> dict[str, Path]:
        """Download a set of bottles with bounded concurrency (fail-fast).

        The per-bottle semaphore caps parallelism. Already-cached bottles return
        immediately; a re-run after a partial failure resumes from cache.

        Args:
            refs: The bottle references to fetch.
            on_progress: A callback function to report download progress.

        Returns:
            A dictionary mapping bottle names to their cached paths.
        """

        async def one(ref: BottleRef) -> tuple[str, Path]:
            return ref.name, await self.fetch(ref, on_progress=on_progress)

        return dict(await asyncio.gather(*(one(r) for r in refs)))

    async def _download(
        self, ref: BottleRef, dest: Path, on_progress: ProgressCb | None
    ) -> Path:
        """Download a bottle and return its cached path.

        Args:
            ref: The bottle reference to download.
            dest: The destination path for the downloaded bottle.
            on_progress: A callback function to report download progress.

        Returns:
            The path to the cached bottle.
        """
        last: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._attempt(ref, dest, on_progress)

            except (httpx.TransportError, _Transient) as exc:
                last = exc
                if attempt < self._max_retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))

        raise DownloadError(ref, f"failed after {self._max_retries} attempts: {last}")

    async def _attempt(
        self, ref: BottleRef, dest: Path, on_progress: ProgressCb | None
    ) -> Path:
        """Download a bottle and return its cached path.

        Args:
            ref: The bottle reference to download.
            dest: The destination path for the downloaded bottle.
            on_progress: A callback function to report download progress.

        Returns:
            The path to the cached bottle.
        """
        if self._client is None:
            raise BrewError(message="HTTP client is not initialised.")

        hasher = hashlib.sha256()
        fd, tmp_name = tempfile.mkstemp(dir=self._cache_dir, suffix=".part")
        tmp = Path(tmp_name)
        downloaded = 0
        try:
            with os.fdopen(fd, "wb") as out:
                async with self._client.stream(
                    "GET",
                    ref.url,
                    headers=self._headers(ref.url),
                    follow_redirects=True,
                ) as resp:
                    if resp.status_code in _RETRYABLE_STATUS:
                        raise _Transient(resp.status_code)

                    resp.raise_for_status()
                    total = int(resp.headers.get("Content-Length", 0)) or None

                    async for chunk in resp.aiter_bytes(_CHUNK):
                        out.write(chunk)
                        hasher.update(chunk)
                        downloaded += len(chunk)
                        if on_progress is not None:
                            on_progress(downloaded, total)

            digest = hasher.hexdigest()
            if digest != ref.sha256:
                raise DownloadError(
                    ref, f"sha256 mismatch: expected {ref.sha256}, got {digest}"
                )
            os.replace(tmp, dest)  # Atomic publish into the content-addressed cache

            return dest

        except httpx.HTTPStatusError as exc:
            raise DownloadError(ref, f"HTTP {exc.response.status_code}") from exc

        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)  # No-op after a successful replace
