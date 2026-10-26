"""Unit tests for the downloader's pure logic."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from brewery.providers.downloader import (
    DEFAULT_GHCR_TOKEN,
    NO_CHECK,
    BottleRef,
    Downloader,
)


def _dl(token: str = DEFAULT_GHCR_TOKEN) -> Downloader:
    """Create a Downloader instance for testing.

    Args:
        token: The GitHub Container Registry token to use.

    Returns:
        A Downloader instance.
    """
    return Downloader(Path("/cache"), token=token)


class TestRequestHeaders:
    """Tests the headers sent to each bottle host."""

    @pytest.mark.parametrize(
        ("token", "url", "expected_auth"),
        [
            pytest.param(
                DEFAULT_GHCR_TOKEN,
                "https://ghcr.io/v2/homebrew/core/foo/blobs/sha256:abc",
                f"Bearer {DEFAULT_GHCR_TOKEN}",
                id="ghcr_default_bearer",
            ),
            pytest.param(
                "custom",
                "https://ghcr.io/v2/x/blobs/sha256:abc",
                "Bearer custom",
                id="ghcr_custom_token",
            ),
            pytest.param(
                "",
                "https://ghcr.io/v2/x/blobs/sha256:abc",
                None,
                id="empty_token_omits_auth",
            ),
            # Presigned CDN / mirror URLs must never receive the ghcr bearer.
            pytest.param(
                DEFAULT_GHCR_TOKEN,
                "https://pkg-containers.githubusercontent.com/blob/obj",
                None,
                id="non_ghcr_host_omits_auth",
            ),
        ],
    )
    def test_headers_authorization(self, token, url, expected_auth) -> None:
        """Test that the Authorization header is scoped to ghcr and honours the token."""
        h = _dl(token=token)._headers(url)
        if expected_auth is None:
            assert "Authorization" not in h
        else:
            assert h["Authorization"] == expected_auth

    def test_headers_ghcr_sets_accept(self) -> None:
        """Test that ghcr is asked for the OCI layer."""
        h = _dl()._headers("https://ghcr.io/v2/x/blobs/sha256:abc")
        assert h["Accept"].startswith("application/vnd.oci.image.layer")

    def test_headers_vendor_host_sends_none(self) -> None:
        """Test that a cask's vendor host gets a plain request."""
        assert _dl()._headers("https://example.org/Foo-1.0.dmg") == {}


class TestBottleRef:
    """Tests the cache key and the value semantics it depends on."""

    def test_cache_path_is_sha_under_cache_dir(self) -> None:
        """Test that the cache path is under the cache directory."""
        dl = Downloader(Path("/var/cache/brewery"))
        assert dl.cache_path("deadbeef") == Path("/var/cache/brewery/deadbeef")

    def test_bottle_ref_is_frozen_and_hashable(self) -> None:
        """Test that BottleRef is frozen and hashable."""
        ref = BottleRef("foo", "https://ghcr.io/...", "abc")
        assert {ref}  # Hashable -> usable as a dict/set key
        with pytest.raises(AttributeError):
            ref.sha256 = "x"  # ty: ignore[invalid-assignment]

    def test_key_is_the_digest(self) -> None:
        """Test that a verified ref is cached under its own digest."""
        assert BottleRef("foo", "https://example.org/foo.zip", "abc").key == "abc"

    def test_no_check_key_is_the_url_digest(self) -> None:
        """Test that a `no_check` ref is cached under sha256(url), per URL."""
        a = BottleRef("foo", "https://example.org/foo-1.zip", NO_CHECK)
        b = BottleRef("foo", "https://example.org/foo-2.zip", NO_CHECK)
        assert a.key == hashlib.sha256(a.url.encode()).hexdigest()
        assert a.key != b.key
