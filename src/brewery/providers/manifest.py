"""Download bottle manifests for extraction/install information."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import orjson

from brewery.core.errors import BrewError
from brewery.core.host import current_platform

GHCR_BASE = "https://ghcr.io/v2/homebrew/core"

_ANON_BEARER = "QQ=="
_INDEX_ACCEPT = "application/vnd.oci.image.index.v1+json"

_TAB_ANNOTATION = "sh.brew.tab"
_DIGEST_ANNOTATION = "sh.brew.bottle.digest"
_PATH_EXEC_ANNOTATION = "sh.brew.path_exec_files"
_INSTALLED_SIZE_ANNOTATION = "sh.brew.bottle.installed_size"


class ManifestError(BrewError):
    """Raised when the manifest can't yield a usable tab.

    Callers treat this as 'fall back to scanning the keg + assembling the
    receipt without tab fields', not as a hard install failure.
    """


@dataclass(frozen=True)
class BottleTabInfo:
    """The build-time tab brew writes (mostly verbatim) into the receipt.

    `arch` and `built_on` are None for `all` bottles; the receipt fills
    arch from the host and writes built_on as null in that case.
    """

    homebrew_version: str
    changed_files: list[str]  # Text-substituted relocation list (relative paths)
    source_modified_time: int
    compiler: str
    runtime_dependencies: list[dict]  # Tab shape: receipt strips compatibility_version
    arch: str | None = None
    built_on: dict[str, object] | None = None
    path_exec_files: list[str] = field(default_factory=list)
    installed_size: int | None = None


def image_formula_name(name: str) -> str:
    """brew's GitHubPackages.image_formula_name: '@' -> '/', lowercased.

    e.g. 'openssl@3' -> 'openssl/3', 'node' -> 'node'.

    Args:
        name: The formula name as it appears in the catalog.

    Returns:
        The GHCR image path segment for the formula.
    """
    return name.lower().replace("@", "/")


def _os_major(os_version: str) -> int | None:
    """Major macOS number from an OCI `os.version` string ('macOS 14.8' -> 14).

    Args:
        os_version: The `os.version` field from an OCI platform descriptor,
            e.g. `'macOS 14.8'`.

    Returns:
        The integer major version, or `None` if the string cannot be parsed.
    """
    parts = os_version.split()
    if not parts:
        return None

    try:
        return int(parts[-1].split(".")[0])

    except ValueError:
        return None


def _os_sort_key(os_version: str) -> tuple[int, ...]:
    """Sortable version tuple from an OCI `os.version` ('macOS 14.8' -> (14, 8)).

    Args:
        os_version: The `os.version` field from an OCI platform descriptor,
            e.g. `'macOS 14.8'`.

    Returns:
        A tuple of integers suitable for comparison, or an empty tuple if the
        string cannot be parsed.
    """
    parts = os_version.split()
    if not parts:
        return ()

    out: list[int] = []
    for n in parts[-1].split("."):
        try:
            out.append(int(n))

        except ValueError:
            out.append(0)

    return tuple(out)


def select_bottle_manifest(
    manifests: list, bottle_sha256: str, *, oci_arch: str, os_major: int
) -> dict | None:
    """Pick the manifest entry for the host's bottle.

    A bottle's blob can be shared across platforms: `:any_skip_relocation` and
    `all` bottles reuse one tarball for several macOS versions (and sometimes
    arches), so several index entries carry the *same* `sh.brew.bottle.digest`
    while each holds its own per-platform `sh.brew.tab` (distinct `built_on`,
    arch, etc.). Matching on the digest alone is ambiguous -- the first match may
    be a different platform's entry -- so we disambiguate by host arch and macOS
    version the way brew selects a bottle. For a normal per-platform bottle the
    digest is unique and the first branch returns immediately.

    Args:
        manifests: The `manifests` array from an OCI image index.
        bottle_sha256: The expected bottle blob digest from the catalog (with or
            without the `sha256:` prefix).
        oci_arch: OCI architecture string for the host, e.g. `'arm64'`.
        os_major: Host macOS major version number, e.g. `14`.

    Returns:
        The matching manifest dict, or `None` if no digest match is found.
    """
    want = bottle_sha256.removeprefix("sha256:").lower()
    matches = [
        m
        for m in manifests
        if isinstance(m, dict)
        and (m.get("annotations") or {}).get(_DIGEST_ANNOTATION, "").lower() == want
    ]

    if len(matches) <= 1:
        return matches[0] if matches else None

    def plat(m: dict) -> dict:
        return m.get("platform") or {}

    pool = [m for m in matches if plat(m).get("architecture") == oci_arch] or matches

    # Exact host major wins (newest point release if several share it); else the
    # newest major not exceeding the host (brew's forward-compatible fallback);
    # else the newest available.
    exact = [m for m in pool if _os_major(plat(m).get("os.version", "")) == os_major]
    if exact:
        return max(exact, key=lambda m: _os_sort_key(plat(m).get("os.version", "")))

    not_newer = [
        m
        for m in pool
        if (maj := _os_major(plat(m).get("os.version", ""))) is not None
        and maj <= os_major
    ]

    if not_newer:
        return max(not_newer, key=lambda m: _os_sort_key(plat(m).get("os.version", "")))

    return max(pool, key=lambda m: _os_sort_key(plat(m).get("os.version", "")))


def manifest_tag(version: str, revision: int = 0, rebuild: int = 0) -> str:
    """OCI tag for the image index: pkg_version, with '_<revision>' when revision > 0.

    Args:
        version: The stable version string from the catalog.
        revision: The formula revision; omit or pass 0 for the base version.
        rebuild: The bottle rebuild counter; appended as `-<rebuild>` when
            non-zero.

    Returns:
        The OCI tag string to use when fetching the image index from GHCR.
    """
    tag = f"{version}_{revision}" if revision else version
    if rebuild:
        tag += f"-{rebuild}"

    return tag


_RETRY_STATUS = {429, 500, 502, 503, 504}


async def _get_index_with_retry(
    client: httpx.AsyncClient, url: str, headers: dict, *, max_retries: int = 3
) -> httpx.Response:
    """GET the manifest index, retrying transient failures with exponential backoff.

    Args:
        client: The shared async HTTP client to use for the request.
        url: The full GHCR manifest URL to fetch.
        headers: Request headers (Authorization, Accept) to include.
        max_retries: Number of additional attempts after the first failure.

    Returns:
        The successful `httpx.Response` object.

    Raises:
        httpx.HTTPStatusError: On a non-retryable HTTP error (e.g. 404).
        httpx.TransportError: If all retry attempts are exhausted due to
            connection-level failures.
    """
    import asyncio
    import random

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(url, headers=headers, follow_redirects=True)
            resp.raise_for_status()

            return resp

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _RETRY_STATUS:
                raise  # 404 etc, are genuine, not transient
            last_exc = exc

        except httpx.TransportError as exc:  # Timeouts, connection resets
            last_exc = exc

        if attempt < max_retries:
            await asyncio.sleep((2**attempt) * 0.5 + random.random() * 0.25)

    assert last_exc is not None
    raise last_exc


async def fetch_bottle_tab(
    client: httpx.AsyncClient,
    *,
    name: str,
    version: str,
    bottle_sha256: str,
    revision: int = 0,
    rebuild: int = 0,
) -> BottleTabInfo:
    """Fetch the ghcr image index and lift the CI tab for the matching bottle.

    Args:
        client: Shared async client (anonymous).
        name: Formula name
        version: Stable version string from the catalog (versions.stable).
        bottle_sha256: The bottle blob sha256 from the catalog.
        revision: Formula revision, if any.

    Returns:
        BottleTabInfo with the bottle-intrinsic receipt fields (+ extras).

    Raises:
        ManifestError: manifest unreachable, no digest match, or tab
            missing/unparseable.
    """
    image = image_formula_name(name)
    tag = manifest_tag(version, revision, rebuild)
    url = f"{GHCR_BASE}/{image}/manifests/{tag}"

    try:
        resp = await _get_index_with_retry(
            client,
            url,
            {
                "Authorization": f"Bearer {_ANON_BEARER}",
                "Accept": _INDEX_ACCEPT,
            },
        )
        index = orjson.loads(resp.content)

    except (httpx.HTTPError, orjson.JSONDecodeError) as exc:
        raise ManifestError(f"manifest fetch failed for {name} {tag}: {exc}") from exc

    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise ManifestError(f"no manifests array in index for {name} {tag}")

    p = current_platform()
    oci_arch = p.arch if p is not None else "amd64"
    os_major = p.macos_major if p is not None else 0
    entry = select_bottle_manifest(
        manifests, bottle_sha256, oci_arch=oci_arch, os_major=os_major
    )

    if entry is None:
        want = bottle_sha256.removeprefix("sha256:").lower()
        raise ManifestError(
            f"no manifest matching bottle digest {want[:12]}… for {name} {tag}"
        )

    ann = entry["annotations"]

    raw_tab = ann.get(_TAB_ANNOTATION)
    if not raw_tab:
        raise ManifestError(f"missing {_TAB_ANNOTATION} for {name} {tag}")

    try:
        tab = orjson.loads(raw_tab)

    except orjson.JSONDecodeError as exc:
        raise ManifestError(
            f"unparseable {_TAB_ANNOTATION} for {name} {tag}: {exc}"
        ) from exc

    # Required tab fields (present on both platform and all bottles)
    try:
        homebrew_version = tab["homebrew_version"]
        source_modified_time = int(tab["source_modified_time"])
        compiler = tab["compiler"]

    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError(
            f"incomplete {_TAB_ANNOTATION} for {name} {tag}: {exc}"
        ) from exc

    # Optional on all bottles: arch and built_on are absent
    arch = tab.get("arch")
    built_on = tab.get("built_on") or None  # Normalise {} -> None

    changed_files = list(tab.get("changed_files") or [])
    runtime_dependencies = list(tab.get("runtime_dependencies") or [])

    path_exec = ann.get(_PATH_EXEC_ANNOTATION)
    path_exec_files = path_exec.split(",") if path_exec else []

    raw_size = ann.get(_INSTALLED_SIZE_ANNOTATION)
    installed_size = int(raw_size) if raw_size else None

    return BottleTabInfo(
        homebrew_version=homebrew_version,
        changed_files=changed_files,
        source_modified_time=source_modified_time,
        compiler=compiler,
        runtime_dependencies=runtime_dependencies,
        arch=arch,
        built_on=built_on,
        path_exec_files=path_exec_files,
        installed_size=installed_size,
    )
