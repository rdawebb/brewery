"""A simple file-based cache with expiration."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Literal, Optional

from rich.console import Console

from brewery.core.config import CACHE_DIR, BreweryENV, get_brewery_env
from brewery.core.decorators import log_operation
from brewery.core.errors import CacheError
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.providers import brew_cask, brew_formula

log: BreweryLogger = get_logger(name=__name__)
console = Console()

_cached_token = None
_token_timestamp = 0

_KIND_VALUES: frozenset[str] = frozenset(k.value for k in PackageKind)
WIDTHS_CACHE: Path = CACHE_DIR / "column_widths.json"


class Cache:
    """A simple file-based cache with expiration."""

    def __init__(self, namespace: str):
        """Initialise the cache for a specific namespace."""
        self.cache_path: Path = CACHE_DIR / namespace
        self.cache_path.mkdir(parents=True, exist_ok=True)
        log.debug(
            event="cache_initialised",
            namespace=namespace,
            path=str(object=self.cache_path),
        )

    def _file(self, key: str) -> Path:
        """Get the file path for a given cache key.

        Args:
            key: The cache key.

        Returns:
            The Path to the cache file.
        """
        return self.cache_path / f"{key}.json"

    def _update_token(self) -> str:
        """Generate a new update token based on the current time.

        Returns:
            A string token representing the current state.
        """
        global _cached_token, _token_timestamp
        now: float = time.time()
        if _cached_token and (now - _token_timestamp) < 1:
            return _cached_token

        brewery: BreweryENV = get_brewery_env()

        def mtime(p: Path) -> int:
            try:
                return int(p.stat().st_mtime)

            except FileNotFoundError:
                return 0

        taps_path: Path = brewery.prefix / "Homebrew" / "Library" / "Taps"

        _cached_token = "-".join(
            str(mtime(p))
            for p in [
                brewery.cellar,
                brewery.caskroom,
                taps_path,
            ]
        )
        _token_timestamp = now

        return _cached_token

    def get(self, key: str) -> Optional[Any]:
        """Get a cached value by key.

        Args:
            key: The cache key.

        Returns:
            The cached value, or None if not found.
        """
        f: Path = self._file(key)
        if not f.exists():
            return None

        try:
            data: Any = json.loads(s=f.read_text())
            token: str = self._update_token()

            if token == data.get("_token"):
                log.info(event="cache_hit", key=key, namespace=self.cache_path.name)
                return data.get("value")

            else:
                log.debug(
                    event="cache_invalid", key=key, namespace=self.cache_path.name
                )
                return None

        except json.JSONDecodeError:
            log.warning(
                event="cache_corrupted",
                key=key,
                namespace=self.cache_path.name,
                exc_info=True,
            )
        except Exception as e:
            log.error(
                event="cache_read_error",
                key=key,
                namespace=self.cache_path.name,
                exc_info=True,
            )
            raise CacheError(
                key=key,
                namespace=self.cache_path.name,
                operation="read",
            ) from e

        return None

    def set(self, key: str, value: Any) -> None:
        """Set a cached value by key.

        Args:
            key: The cache key.
            value: The value to cache.
        """
        f: Path = self._file(key)
        now = int(time.time())
        token: str = self._update_token()
        start: float = time.perf_counter()

        try:
            f.write_text(
                data=json.dumps(obj={"_ts": now, "_token": token, "value": value})
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                event="cache_set",
                key=key,
                namespace=self.cache_path.name,
                duration_ms=duration_ms,
            )

        except Exception as e:
            log.error(
                event="cache_write_error",
                key=key,
                namespace=self.cache_path.name,
                error=str(object=e),
                exc_info=True,
            )
            raise CacheError(
                key=key,
                namespace=self.cache_path.name,
                operation="write",
                path=str(object=f),
            ) from e


class CacheManager:
    """Manages all repository cache operations."""

    def __init__(self, cache: Cache):
        """Initialise CacheManager with a Cache instance

        Args:
            cache: A Cache instance to use for storage.
        """
        self.cache: Cache = cache
        log.debug(event="cache_manager_initialised")

    @staticmethod
    def _cache_keys(kind_value: str) -> tuple[str, str]:
        """Return the cache keys based on kind.

        Args:
            kind_value: The package kind to get the cache keys for.

        Returns:
            Tuple of list_key and map_key.
        """
        return f"installed_{kind_value}", f"installed_map_{kind_value}"

    async def load_packages(self, kind: Optional[PackageKind] = None) -> list[Package]:
        """Load packages from cache.

        Args:
            kind: Optional filter for package kind.

        Returns:
            List of cached packages, or empty list if not cached.
        """
        list_key, _ = self._cache_keys(kind_value=kind.value if kind else "all")

        try:
            cached_data: Any = self.cache.get(key=list_key)

            if cached_data is not None and isinstance(cached_data, list):
                pkgs: list[Package] = [
                    Package.package_from_dict(data=d) for d in cached_data
                ]
                log.debug(event="cache_load_success", key=list_key, count=len(pkgs))

                return pkgs

            return []

        except CacheError:
            raise

        except Exception as e:
            log.error(
                event="cache_load_error",
                key=list_key,
                error=str(object=e),
                exc_info=True,
            )
            return []

    @log_operation(event_prefix="refresh_packages", log_args=["kind"])
    async def refresh_packages(
        self, kind: Optional[PackageKind] = None
    ) -> list[Package]:
        """Refresh cache by fetching from providers.

        Fetches packages from brew providers and updates all related caches.

        Args:
            kind: Optional filter for package kind.

        Returns:
            List of fetched packages.
        """
        pkgs: list[Package] = []
        tasks: list = []

        # Gather tasks based on kind
        if kind in (None, PackageKind.FORMULA):
            tasks.append(brew_formula.list_installed())
        if kind in (None, PackageKind.CASK):
            tasks.append(brew_cask.list_installed())

        if tasks:
            results: list = await asyncio.gather(*tasks)
            for result in results:
                pkgs.extend(result)

        pkgs.sort(key=lambda p: (p.kind.value, p.name.lower()))

        # Update caches
        await self._update_caches(kind=kind, pkgs=pkgs)

        return pkgs

    async def invalidate_and_refresh(self, kind: PackageKind) -> None:
        """Invalidate cache for a kind and refresh all related caches.

        Args:
            kind: The package kind to invalidate.
        """
        log.info(event="cache_invalidate_start", kind=kind.value)

        # Delete both specific and 'all' caches
        for suffix in [kind.value, "all"]:
            for key in self._cache_keys(kind_value=suffix):
                f: Path = self.cache._file(key)
                if f.exists():
                    f.unlink()
                    log.debug(event="cache_file_deleted", key=key)

        # Refresh both specific and 'all' caches
        await self.refresh_packages(kind=None)

        log.info(event="cache_invalidate_complete", kind=kind.value)

    async def update_packages(
        self,
        packages: Package | list[Package],
        action: Literal["add", "remove", "update"],
    ) -> None:
        """Update one or more package entries in cache.

        Args:
            packages: A single Package or list of Packages to update.
            action: "add", "remove", or "update".
        """
        pkg_list: list[Package] = (
            [packages] if isinstance(packages, Package) else packages
        )

        kinds_to_update: set[str] = {p.kind.value for p in pkg_list} | {"all"}
        pkg_names: set[str] = {p.name for p in pkg_list}

        for suffix in kinds_to_update:
            list_key, map_key = self._cache_keys(kind_value=suffix)

            try:
                cached_list: list | None = self.cache.get(key=list_key)
                cached_map: dict | None = self.cache.get(key=map_key)

                if cached_list is None and cached_map is not None:
                    cached_list = list(cached_map.values())
                    log.warning(event="cache_list_rebuilt_from_map", key=list_key)
                elif cached_map is None and cached_list is not None:
                    cached_map = {p["name"]: p for p in cached_list}
                    log.warning(event="cache_map_rebuilt_from_list", key=map_key)
                elif cached_list is None and cached_map is None:
                    log.warning(event="rebuilding_missing_cache", key=list_key)
                    await self.refresh_packages(
                        kind=PackageKind(suffix) if suffix != "all" else None
                    )
                    continue

                if cached_list is None or cached_map is None:
                    log.warning(event="cache_missing", key=list_key)
                    await self.refresh_packages(
                        kind=PackageKind(suffix) if suffix != "all" else None
                    )
                    continue

                if action == "remove":
                    cached_list[:] = [
                        p for p in cached_list if p.get("name") not in pkg_names
                    ]

                    for name in pkg_names:
                        cached_map.pop(name, None)

                elif action in ("add", "update"):
                    cached_list[:] = [
                        p for p in cached_list if p.get("name") not in pkg_names
                    ]

                    for pkg in pkg_list:
                        pkg_dict: dict = pkg.to_serializable_dict()
                        cached_list.append(pkg_dict)
                        cached_map[pkg.name] = pkg_dict

                if pkg_list and cached_list:
                    cached_list.sort(
                        key=lambda p: (p.get("kind", ""), p.get("name", "").lower())
                    )

                self.cache.set(key=list_key, value=cached_list)
                self.cache.set(key=map_key, value=cached_map)

            except Exception as e:
                log.error(
                    event="cache_update_failed",
                    key=list_key,
                    error=str(object=e),
                    exc_info=True,
                )

    async def get_details_from_cache(
        self, name: str, kind: PackageKind
    ) -> Optional[Package]:
        """Fetch package details from cache with fallback chain.

        Tries:
        1. Map cache (fastest)
        2. List cache (fallback)
        3. Returns None if not found

        Args:
            name: Package name.
            kind: Package kind.

        Returns:
            Package instance if found, None otherwise.
        """
        list_key, map_key = self._cache_keys(kind_value=kind.value)

        # Try map cache first
        try:
            cached_map: dict | None = self.cache.get(key=map_key)
            if cached_map is not None and name in cached_map:
                log.debug(event="cache_hit_map", key=map_key, package=name)
                return Package.package_from_dict(data=cached_map[name])
        except Exception as e:
            log.debug(event="cache_lookup_map_failed", error=str(object=e))

        # Try list cache
        try:
            cached_list: dict | None = self.cache.get(key=list_key)
            if cached_list is not None:
                for pkg_data in cached_list:
                    if pkg_data.get("name") == name:
                        log.debug(event="cache_hit_list", key=list_key, package=name)
                        return Package.package_from_dict(data=pkg_data)
        except Exception as e:
            log.debug(event="cache_lookup_list_failed", error=str(object=e))

        log.debug(event="cache_miss", package=name, kind=kind.value)
        return None

    async def refresh_outdated_status(self, outdated_entries: list) -> None:
        """Refresh outdated package status in cache.

        Args:
            outdated_entries: The fetched list of outdated entries from Brew.
        """
        try:
            all_pkgs: list[Package] = await self.load_packages(kind=None)
            if not all_pkgs:
                all_pkgs: list[Package] = await self.refresh_packages(kind=None)

            outdated_map: dict = {e["name"]: e for e in outdated_entries}
            changed: list[Package] = []

            for pkg in all_pkgs:
                if pkg.name in outdated_map:
                    entry = outdated_map[pkg.name]
                    pkg.status |= PackageStatus.OUTDATED
                    pkg.metadata["latest_version"] = entry.get("current_version")
                    changed.append(pkg)

            if changed:
                await self.update_packages(packages=changed, action="update")

            log.info(event="outdated_cache_updated", count=len(outdated_entries))

        except Exception as e:
            log.error(
                event="outdated_cache_update_failed", error=str(object=e), exc_info=True
            )

    async def _update_caches(
        self, kind: Optional[PackageKind], pkgs: list[Package]
    ) -> None:
        """Internal helper: update cache files after refresh.

        Args:
            kind: The kind filter used in refresh.
            pkgs: The packages that were fetched.
        """
        if kind:
            suffixes: list[str] = [kind.value]
        else:
            suffixes: list[str] = ["all"] + [k.value for k in PackageKind]

        pkgs_dicts: list = [p.to_serializable_dict() for p in pkgs]

        for suffix in suffixes:
            list_key, map_key = self._cache_keys(kind_value=suffix)

            filtered = (
                [p for p in pkgs_dicts if p.get("kind") == suffix]
                if suffix in _KIND_VALUES
                else pkgs_dicts
            )

            filtered_map: dict = {p["name"]: p for p in filtered}

            try:
                self.cache.set(key=list_key, value=filtered)
                self.cache.set(key=map_key, value=filtered_map)
                log.debug(event="cache_updated", list_key=list_key, count=len(pkgs))

            except Exception as e:
                log.error(
                    event="cache_update_failed",
                    list_key=list_key,
                    error=str(object=e),
                    exc_info=True,
                )
