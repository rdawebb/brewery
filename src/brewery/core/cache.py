"""A simple file-based cache with expiration."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from rich.console import Console
from structlog.typing import FilteringBoundLogger

from brewery.core.config import CACHE_DIR, BreweryENV, get_brewery_env
from brewery.core.errors import CacheError, TransientError
from brewery.core.logging import get_logger
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.providers import brew_cask, brew_formula, brew_outdated

log: FilteringBoundLogger = get_logger(name=__name__)
console = Console()

_cached_token = None
_token_timestamp = 0


class Cache:
    """A simple file-based cache with expiration."""

    def __init__(self, namespace: str):
        """Initialise the cache for a specific namespace."""
        self.cache_path: Path = CACHE_DIR / namespace
        self.cache_path.mkdir(parents=True, exist_ok=True)
        self._cached_token = None
        self._token_timestamp = 0
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
        now: int | float = time.time()
        if _cached_token and (now - _token_timestamp) < 1:
            return _cached_token

        brewery: BreweryENV = get_brewery_env()

        def mtime(p: Path) -> int:
            try:
                return int(p.stat().st_mtime)

            except FileNotFoundError:
                return 0

        cellar_mtime: int = mtime(p=brewery.cellar)
        caskroom_mtime: int = mtime(p=brewery.caskroom)

        _cached_token = f"{cellar_mtime}-{caskroom_mtime}"
        _token_timestamp = now

        return _cached_token

    def get_or_set(
        self,
        key: str,
        ttl: Optional[int],
        loader: Callable[[], Any],
        allow_stale: bool = False,
    ) -> Any:
        """Get a cached value or set it using the loader function.

        Args:
            key: The cache key.
            ttl: Time-to-live in seconds, or None for no expiration.
            loader: A callable that returns the value to cache.

        Returns:
            Cached or fresh value.
        """
        f: Path = self._file(key)
        now = int(time.time())
        token: str = self._update_token()
        start: int | float = time.perf_counter()
        stale_data = None

        if f.exists():
            try:
                data: Any = json.loads(s=f.read_text())
                ttl_valid: Literal[True] | Any = ttl is None or (
                    now - data.get("_ts", 0) < ttl
                )
                if ttl_valid and data.get("_token") == token:
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    age_seconds: Any = now - data["_ts"]
                    log.info(
                        event="cache_hit",
                        key=key,
                        namespace=self.cache_path.name,
                        age_seconds=age_seconds,
                        duration_ms=duration_ms,
                    )

                    return data.get("value")

                else:
                    reason: Literal["expired", "token_mismatch"] = (
                        "expired"
                        if (now - data.get("_ts", 0) >= ttl)
                        else "token_mismatch"
                    )
                    log.debug(
                        event="cache_invalid",
                        key=key,
                        namespace=self.cache_path.name,
                        reason=reason,
                    )
                    if allow_stale:
                        stale_data: Any = data.get("value")

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

        log.info(event="cache_miss", key=key, namespace=self.cache_path.name)

        try:
            value: Any = loader()

        except TransientError as e:
            if allow_stale and stale_data is not None:
                age_seconds: Any = now - data.get("_ts", now)
                log.warning(
                    event="cache_fallback_stale",
                    key=key,
                    namespace=self.cache_path.name,
                    age_seconds=age_seconds,
                    error=str(object=e),
                )
                console.print(
                    "⚠️ Using cached data due to temporary error (may be outdated).\n",
                    style="bold yellow",
                )
                return stale_data

            else:
                raise

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

        return value

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
        start: int | float = time.perf_counter()

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

    async def load_packages(self, kind: Optional[PackageKind] = None) -> list[Package]:
        """Load packages from cache.

        Args:
            kind: Optional filter for package kind.

        Returns:
            List of cached packages, or empty list if not cached.
        """
        cache_key = f"installed_{kind.value if kind else 'all'}"

        try:
            cached_data: Any = self.cache.get(key=cache_key)

            if cached_data is not None and isinstance(cached_data, list):
                pkgs: list[Package] = [
                    Package.package_from_dict(data=d) for d in cached_data
                ]
                log.debug(event="cache_load_success", key=cache_key, count=len(pkgs))
                return pkgs

            return []

        except Exception as e:
            log.error(
                event="cache_load_error",
                key=cache_key,
                error=str(object=e),
                exc_info=True,
            )
            return []

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
        start: float = time.perf_counter()

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

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            event="cache_refresh_complete",
            kind=kind.value if kind else "all",
            count=len(pkgs),
            duration_ms=duration_ms,
        )

        return pkgs

    async def invalidate_and_refresh(self, kind: PackageKind) -> None:
        """Invalidate cache for a kind and refresh all related caches.

        Args:
            kind: The package kind to invalidate.
        """
        log.info(event="cache_invalidate_start", kind=kind.value)

        # Delete both specific and 'all' caches
        for suffix in [kind.value, "all"]:
            for prefix in ("installed_", "installed_map_"):
                key = f"{prefix}{suffix}"
                f: Path = self.cache._file(key)
                if f.exists():
                    f.unlink()
                    log.debug(event="cache_file_deleted", key=key)

        # Refresh both specific and 'all' caches
        await self.refresh_packages(kind=kind)
        await self.refresh_packages(kind=None)

        log.info(event="cache_invalidate_complete", kind=kind.value)

    async def update_single(
        self,
        name: str,
        kind: PackageKind,
        action: Literal["add", "remove", "update"],
        pkg: Optional[Package] = None,
    ) -> None:
        """Update a single package entry in cache.

        Updates both the specific kind cache (e.g., 'installed_formula')
        and the combined 'installed_all' cache.

        Args:
            name: Package name.
            kind: Package kind.
            action: "add", "remove", or "update".
            pkg: Package instance (required for add/update).
        """
        log.info(
            event="cache_update_single_start",
            package=name,
            action=action,
            kind=kind.value,
        )

        for suffix in [kind.value, "all"]:
            list_key = f"installed_{suffix}"
            map_key = f"installed_map_{suffix}"

            try:
                cached_list = self.cache.get(key=list_key)
                cached_map = self.cache.get(key=map_key)

                if cached_list is None or cached_map is None:
                    log.debug(
                        event="cache_update_skipped_missing",
                        list_key=list_key,
                        map_key=map_key,
                    )
                    continue

                if action == "remove":
                    cached_map.pop(name, None)
                    cached_list: list = [
                        p for p in cached_list if p.get("name") != name
                    ]

                elif action in ("add", "update") and pkg:
                    pkg_dict: dict = pkg.to_serializable_dict()
                    cached_map[name] = pkg_dict

                    # Remove old entry if updating
                    cached_list: list = [
                        p for p in cached_list if p.get("name") != name
                    ]
                    cached_list.append(pkg_dict)

                    # Re-sort
                    cached_list.sort(
                        key=lambda p: (p.get("kind", ""), p.get("name", "").lower())
                    )

                self.cache.set(key=list_key, value=cached_list)
                self.cache.set(key=map_key, value=cached_map)

                log.debug(event="cache_update_success", key=list_key)

            except Exception as e:
                log.error(
                    event="cache_update_failed",
                    list_key=list_key,
                    error=str(object=e),
                    exc_info=True,
                )

        log.info(event="cache_update_single_complete", package=name)

    async def update_batch(
        self, packages: list[Package], kinds: list[PackageKind]
    ) -> None:
        """Update multiple package entries in cache.

        Args:
            packages: List of Package instances to update.
            kinds: List of PackageKind to update.
        """
        log.info(event="cache_update_batch_start", count=len(packages))

        for kind in kinds:
            list_key = f"installed_{kind.value}"
            map_key = f"installed_map_{kind.value}"

            try:
                cached_list = self.cache.get(key=list_key)
                cached_map = self.cache.get(key=map_key)

                if cached_list is None or cached_map is None:
                    log.debug(event="cache_update_batch_skipped", kind=kind.value)
                    continue

                # Update entries
                for pkg in packages:
                    if pkg.kind == kind:
                        pkg_dict: dict = pkg.to_serializable_dict()
                        cached_map[pkg.name] = pkg_dict
                        cached_list: list = [
                            p for p in cached_list if p.get("name") != pkg.name
                        ]
                        cached_list.append(pkg_dict)

                # Re-sort
                cached_list.sort(
                    key=lambda p: (p.get("kind", ""), p.get("name", "").lower())
                )

                self.cache.set(key=list_key, value=cached_list)
                self.cache.set(key=map_key, value=cached_map)

                log.debug(event="cache_update_batch_success", kind=kind.value)

            except Exception as e:
                log.error(
                    event="cache_update_batch_failed",
                    kind=kind.value,
                    error=str(object=e),
                )

        log.info(event="cache_update_batch_complete", count=len(packages))

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
        map_key = f"installed_map_{kind.value}"
        list_key = f"installed_{kind.value}"

        # Try map cache first
        try:
            cached_map = self.cache.get(key=map_key)
            if cached_map is not None and name in cached_map:
                log.debug(event="cache_hit_map", key=map_key, package=name)
                return Package.package_from_dict(data=cached_map[name])
        except Exception as e:
            log.debug(event="cache_lookup_map_failed", error=str(object=e))

        # Try list cache
        try:
            cached_list = self.cache.get(key=list_key)
            if cached_list is not None:
                for pkg_data in cached_list:
                    if pkg_data.get("name") == name:
                        log.debug(event="cache_hit_list", key=list_key, package=name)
                        return Package.package_from_dict(data=pkg_data)
        except Exception as e:
            log.debug(event="cache_lookup_list_failed", error=str(object=e))

        log.debug(event="cache_miss", package=name, kind=kind.value)
        return None

    async def refresh_outdated_status(self) -> None:
        """Refresh outdated package status in background.

        Fetches outdated packages and updates the cache with latest versions.
        """
        try:
            log.info(event="cache_outdated_refresh_start")

            outdated_entries: list = await brew_outdated.fetch_outdated()
            outdated_map: dict = {entry["name"]: entry for entry in outdated_entries}

            # Load all packages and mark outdated ones
            all_pkgs: list[Package] = await self.load_packages(kind=None)
            if not all_pkgs:
                # Try refresh if not in cache
                all_pkgs: list[Package] = await self.refresh_packages(kind=None)

            updated_pkgs: list = []
            for pkg in all_pkgs:
                if pkg.name in outdated_map:
                    entry: dict = outdated_map[pkg.name]
                    pkg.status |= PackageStatus.OUTDATED
                    if pkg.metadata:
                        pkg.metadata["latest_version"] = entry.get("current_version")

                updated_pkgs.append(pkg)

            # Update cache
            cache_key = "installed_all"
            pkgs_dicts: list = [pkg.to_serializable_dict() for pkg in updated_pkgs]
            self.cache.set(key=cache_key, value=pkgs_dicts)

            log.info(
                event="cache_outdated_refresh_complete", count=len(outdated_entries)
            )

        except Exception as e:
            log.error(
                event="cache_outdated_refresh_failed",
                error=str(object=e),
                exc_info=True,
            )

    async def _update_caches(
        self, kind: Optional[PackageKind], pkgs: list[Package]
    ) -> None:
        """Internal helper: update cache files after refresh.

        Args:
            kind: The kind filter used in refresh.
            pkgs: The packages that were fetched.
        """
        # Update specific kind cache
        if kind is not None:
            list_key = f"installed_{kind.value}"
            map_key = f"installed_map_{kind.value}"
        else:
            list_key = "installed_all"
            map_key = "installed_map_all"

        pkgs_dicts: list = [p.to_serializable_dict() for p in pkgs]
        mapping: dict = {p["name"]: p for p in pkgs_dicts}

        try:
            self.cache.set(key=list_key, value=pkgs_dicts)
            self.cache.set(key=map_key, value=mapping)
            log.debug(event="cache_updated", list_key=list_key, count=len(pkgs))
        except Exception as e:
            log.error(
                event="cache_update_failed",
                list_key=list_key,
                error=str(object=e),
                exc_info=True,
            )
