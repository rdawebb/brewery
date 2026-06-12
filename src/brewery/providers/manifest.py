"""Download bottle manifests for extraction/install information."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import orjson

from brewery.core.errors import BrewError

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

    ``arch`` and ``built_on`` are None for ``all`` bottles; the receipt fills
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
    """
    return name.lower().replace("@", "/")


def manifest_tag(version: str, revision: int = 0) -> str:
    """OCI tag for the image index: pkg_version, with '_<revision>' when revision > 0."""
    return f"{version}_{revision}" if revision else version


async def fetch_bottle_tab(
    client: httpx.AsyncClient,
    *,
    name: str,
    version: str,
    bottle_sha256: str,
    revision: int = 0,
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
    tag = manifest_tag(version, revision)
    url = f"{GHCR_BASE}/{image}/manifests/{tag}"

    try:
        resp = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {_ANON_BEARER}",
                "Accept": _INDEX_ACCEPT,
            },
            follow_redirects=True,
        )
        resp.raise_for_status()
        index = orjson.loads(resp.content)

    except (httpx.HTTPError, orjson.JSONDecodeError) as exc:
        raise ManifestError(f"manifest fetch failed for {name} {tag}: {exc}") from exc

    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise ManifestError(f"no manifests array in index for {name} {tag}")

    want = bottle_sha256.removeprefix("sha256:").lower()
    entry = next(
        (
            m
            for m in manifests
            if isinstance(m, dict)
            and (m.get("annotations") or {}).get(_DIGEST_ANNOTATION, "").lower() == want
        ),
        None,
    )

    if entry is None:
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

    # Optional on all bottles: arch and built_on are simply absent there.
    arch = tab.get("arch")
    built_on = tab.get("built_on") or None  # normalize {} -> None

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
