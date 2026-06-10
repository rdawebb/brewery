"""Unit tests for the downloader's pure logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from brewery.providers.downloader import DEFAULT_GHCR_TOKEN, BottleRef, Downloader

pytestmark = pytest.mark.unit


def _dl(token: str = DEFAULT_GHCR_TOKEN) -> Downloader:
    """Create a Downloader instance for testing.

    Args:
        token: The GitHub Container Registry token to use.

    Returns:
        A Downloader instance.
    """
    return Downloader(Path("/cache"), token=token)


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
def test_headers_authorization(token, url, expected_auth) -> None:
    """Test that the Authorization header is scoped to ghcr and honours the token."""
    h = _dl(token=token)._headers(url)
    if expected_auth is None:
        assert "Authorization" not in h
    else:
        assert h["Authorization"] == expected_auth


@pytest.mark.parametrize(
    "url",
    [
        "https://ghcr.io/v2/x/blobs/sha256:abc",
        "https://example.org/some/mirror.tar.gz",
    ],
)
def test_headers_always_sets_accept(url) -> None:
    """Test that the Accept header is always set regardless of host."""
    assert _dl()._headers(url)["Accept"].startswith("application/vnd.oci.image.layer")


def test_cache_path_is_sha_under_cache_dir() -> None:
    """Test that the cache path is under the cache directory."""
    dl = Downloader(Path("/var/cache/brewery"))
    assert dl.cache_path("deadbeef") == Path("/var/cache/brewery/deadbeef")


def test_bottle_ref_is_frozen_and_hashable() -> None:
    """Test that BottleRef is frozen and hashable."""
    ref = BottleRef("foo", "https://ghcr.io/...", "abc")
    assert {ref}  # Hashable -> usable as a dict/set key
    with pytest.raises(AttributeError):
        ref.sha256 = "x"  # ty: ignore[invalid-assignment]
