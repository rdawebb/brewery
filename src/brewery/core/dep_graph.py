"""Dependency graph caching functionality"""

from __future__ import annotations

from brewery.core.cache import Cache

_DEP_CACHE_KEY = "dep_graph"


def build_dep_graph(
    packages_dicts: list[dict],
    cache: Cache,
) -> dict[str, list[str]]:
    """Build and cache a name → direct-deps adjacency dict.

    Args:
        packages_dicts: A list of package dictionaries.
        cache: The cache to use for storing the dependency graph.

    Returns:
        A dictionary mapping package names to their direct dependencies.
    """
    cached = cache.get(_DEP_CACHE_KEY)
    if cached is not None:
        return cached

    graph: dict[str, list[str]] = {
        p["name"]: [d["name"] for d in p.get("deps", [])] for p in packages_dicts
    }

    cache.set(_DEP_CACHE_KEY, graph)

    return graph
