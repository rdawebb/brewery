"""Tests for the bottle manifest provider."""

from __future__ import annotations

import httpx
import orjson
import pytest

import brewery.providers.manifest as m
from brewery.providers.manifest import BottleTabInfo, ManifestError, fetch_bottle_tab

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

DIGEST = "ab" * 32  # 64-hex bare sha256 (matches catalog form)
OTHER_DIGEST = "cd" * 32

TAB_PLATFORM = {
    "homebrew_version": "5.1.5-3-g9f7d5c5",
    "changed_files": ["bin/c_rehash", "lib/pkgconfig/libssl.pc"],
    "source_modified_time": 1775564277,
    "compiler": "clang",
    "runtime_dependencies": [
        {
            "full_name": "ca-certificates",
            "version": "2026-03-19",
            "revision": 0,
            "bottle_rebuild": 0,
            "pkg_version": "2026-03-19",
            "declared_directly": True,
            "compatibility_version": 1,
        }
    ],
    "arch": "x86_64",
    "built_on": {
        "os": "Macintosh",
        "os_version": "macOS 15.7",
        "cpu_family": "penryn",
        "xcode": "26.3",
        "clt": "26.3.0.0.1.1771626560",
        "preferred_perl": "5.34",
    },
}

TAB_ALL = {
    "homebrew_version": "5.1.11-89-g34257b4",
    "changed_files": [],
    "source_modified_time": 1778728322,
    "compiler": "gcc-12",
    "runtime_dependencies": [],
    # no arch, no built_on (all bottle)
}


def _index(entries: list[dict]) -> bytes:
    """Build an OCI image index. Each entry: {digest, tab, extra}.

    Args:
        entries: The entries to include in the index.

    Returns:
        bytes: The serialized OCI image index.
    """
    manifests = []
    for e in entries:
        ann = {"sh.brew.bottle.digest": e["digest"]}
        tab = e.get("tab")
        if tab is not None:
            ann["sh.brew.tab"] = (
                tab if isinstance(tab, str) else orjson.dumps(tab).decode()
            )
        ann.update(e.get("extra", {}))
        manifests.append(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "annotations": ann,
            }
        )
    return orjson.dumps({"schemaVersion": 2, "manifests": manifests})


def _handler(body: bytes, *, status: int = 200, requests: list | None = None):
    """Create a request handler that returns a mocked HTTP response.

    Args:
        body (bytes): The response body.
        status (int, optional): The HTTP status code. Defaults to 200.
        requests (list | None, optional): A list to append the requests to. Defaults to None.

    Returns:
        callable: The request handler.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        """Handle an HTTP request.

        Args:
            req (httpx.Request): The HTTP request.

        Returns:
            httpx.Response: The mocked HTTP response.
        """
        if requests is not None:
            requests.append(req)
        return httpx.Response(status, content=body)

    return handler


async def _fetch(
    handler, *, name="openssl@3", version="3.6.2", sha=DIGEST, revision=0
) -> BottleTabInfo:
    """Fetch the bottle tab information for a specific formula version.

    Args:
        handler: The request handler.
        name (str, optional): The formula name. Defaults to "openssl@3".
        version (str, optional): The formula version. Defaults to "3.6.2".
        sha (str, optional): The bottle SHA256 digest. Defaults to DIGEST.
        revision (int, optional): The formula revision. Defaults to 0.

    Returns:
        BottleTabInfo: The fetched bottle tab information.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await fetch_bottle_tab(
            client, name=name, version=version, bottle_sha256=sha, revision=revision
        )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("openssl@3", "openssl/3"),
        ("node", "node"),
        ("openssl@3.6", "openssl/3.6"),
        ("Foo", "foo"),
    ],
)
async def test_image_formula_name(name, expected) -> None:
    """Test the image formula name formatting."""
    assert m.image_formula_name(name) == expected


@pytest.mark.parametrize(
    "version,revision,expected",
    [
        ("3.6.2", 0, "3.6.2"),
        ("3.6.2", 2, "3.6.2_2"),
    ],
)
async def test_manifest_tag(version, revision, expected) -> None:
    """Test the manifest tag formatting."""
    assert m.manifest_tag(version, revision) == expected


async def test_fetch_platform_bottle_tab() -> None:
    """Test fetching the platform bottle tab information."""
    info = await _fetch(_handler(_index([{"digest": DIGEST, "tab": TAB_PLATFORM}])))
    assert isinstance(info, BottleTabInfo)
    assert info.homebrew_version == "5.1.5-3-g9f7d5c5"
    assert info.changed_files == ["bin/c_rehash", "lib/pkgconfig/libssl.pc"]
    assert info.source_modified_time == 1775564277
    assert info.compiler == "clang"
    assert info.arch == "x86_64"
    assert info.built_on is not None
    assert info.built_on["cpu_family"] == "penryn"

    # Deps pass through in tab shape (compatibility_version kept; receipt strips it)
    assert info.runtime_dependencies[0]["full_name"] == "ca-certificates"
    assert info.runtime_dependencies[0]["compatibility_version"] == 1


async def test_fetch_all_bottle_tab_has_no_arch_or_built_on() -> None:
    """Test fetching the all bottle tab information."""
    info = await _fetch(_handler(_index([{"digest": DIGEST, "tab": TAB_ALL}])))
    assert info.arch is None
    assert info.built_on is None
    assert info.compiler == "gcc-12"
    assert info.changed_files == []
    assert info.runtime_dependencies == []


async def test_supplementary_annotations_parsed() -> None:
    """Test parsing supplementary annotations."""
    entry = {
        "digest": DIGEST,
        "tab": TAB_PLATFORM,
        "extra": {
            "sh.brew.path_exec_files": "bin/openssl,bin/c_rehash",
            "sh.brew.bottle.installed_size": "123456",
        },
    }
    info = await _fetch(_handler(_index([entry])))
    assert info.path_exec_files == ["bin/openssl", "bin/c_rehash"]
    assert info.installed_size == 123456


async def test_missing_supplementary_annotations_default() -> None:
    """Test missing supplementary annotations default values."""
    info = await _fetch(_handler(_index([{"digest": DIGEST, "tab": TAB_PLATFORM}])))
    assert info.path_exec_files == []
    assert info.installed_size is None


async def test_selects_matching_digest_among_many() -> None:
    """Test selecting the matching digest among many."""
    body = _index(
        [
            {"digest": OTHER_DIGEST, "tab": TAB_ALL},
            {"digest": DIGEST, "tab": TAB_PLATFORM},
        ]
    )
    info = await _fetch(_handler(body), sha=DIGEST)
    assert info.compiler == "clang"  # Picked the platform entry, not the all one


async def test_digest_match_strips_sha256_prefix_and_is_case_insensitive() -> None:
    """Test digest match strips sha256 prefix and is case insensitive."""
    body = _index(
        [{"digest": DIGEST.upper(), "tab": TAB_PLATFORM}]
    )  # Uppercase annotation
    info = await _fetch(
        _handler(body), sha="sha256:" + DIGEST
    )  # Prefixed, lowercase input
    assert info.compiler == "clang"


async def test_no_digest_match_raises() -> None:
    """Test no digest match raises an error."""
    body = _index([{"digest": OTHER_DIGEST, "tab": TAB_PLATFORM}])
    with pytest.raises(ManifestError, match="no manifest matching"):
        await _fetch(_handler(body), sha=DIGEST)


async def test_request_url_and_auth_headers() -> None:
    """Test request URL and authorization headers."""
    reqs: list = []
    await _fetch(
        _handler(_index([{"digest": DIGEST, "tab": TAB_PLATFORM}]), requests=reqs),
        name="openssl@3",
        version="3.6.2",
        revision=0,
    )
    req = reqs[0]
    assert str(req.url) == f"{m.GHCR_BASE}/openssl/3/manifests/3.6.2"
    assert req.headers["Authorization"] == "Bearer QQ=="
    assert "image.index" in req.headers["Accept"]


async def test_http_error_raises_manifest_error() -> None:
    """Test HTTP error raises a manifest error."""
    with pytest.raises(ManifestError, match="manifest fetch failed"):
        await _fetch(_handler(b"", status=404))


async def test_unparseable_index_raises() -> None:
    """Test unparseable index raises a manifest error."""
    with pytest.raises(ManifestError, match="manifest fetch failed"):
        await _fetch(_handler(b"this is not json"))


async def test_no_manifests_array_raises() -> None:
    """Test no manifests array raises a manifest error."""
    body = orjson.dumps({"schemaVersion": 2})  # No 'manifests'
    with pytest.raises(ManifestError, match="no manifests array"):
        await _fetch(_handler(body))


async def test_missing_tab_annotation_raises() -> None:
    """Test missing tab annotation raises a manifest error."""
    body = _index([{"digest": DIGEST, "tab": None}])  # Digest matches, but no tab
    with pytest.raises(ManifestError, match="missing sh.brew.tab"):
        await _fetch(_handler(body))


async def test_unparseable_tab_raises() -> None:
    """Test unparseable tab raises a manifest error."""
    body = _index([{"digest": DIGEST, "tab": "{not valid json"}])
    with pytest.raises(ManifestError, match="unparseable sh.brew.tab"):
        await _fetch(_handler(body))


async def test_incomplete_tab_missing_required_field_raises() -> None:
    """Test incomplete tab missing required field raises a manifest error."""
    bad = {k: v for k, v in TAB_PLATFORM.items() if k != "compiler"}
    body = _index([{"digest": DIGEST, "tab": bad}])
    with pytest.raises(ManifestError, match="incomplete sh.brew.tab"):
        await _fetch(_handler(body))
