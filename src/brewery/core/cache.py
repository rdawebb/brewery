"""Token-invalidated file-based cache and installed-state cache manager"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import orjson

from brewery.core.catalog import Catalog
from brewery.core.config import CACHE_DIR, BreweryENV, get_brewery_env
from brewery.core.errors import CacheError
from brewery.core.fs_state import (
    InstalledRecord,
    attach_sizes,
    record_from_cache,
    records_to_cache,
    scan_installed,
)
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.merge import merge
from brewery.core.models import Package, PackageKind

log: BreweryLogger = get_logger(name=__name__)

_cached_token = None
_token_timestamp = 0

WIDTHS_CACHE: Path = CACHE_DIR / "column_widths.json"


class Cache:
    """A simple file-based cache with mtime-token expiration."""

    def __init__(self, namespace: str) -> None:
        """Initialise the cache for a specific namespace.

        Args:
            namespace: The cache namespace.
        """
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
            data: Any = orjson.loads(f.read_bytes())
            token: str = self._update_token()

            if token == data.get("_token"):
                log.info(event="cache_hit", key=key, namespace=self.cache_path.name)
                return data.get("value")

            else:
                log.debug(
                    event="cache_invalid", key=key, namespace=self.cache_path.name
                )
                return None

        except orjson.JSONDecodeError:
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
            f.write_bytes(orjson.dumps({"_ts": now, "_token": token, "value": value}))
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

    def delete(self, key: str) -> None:
        """Delete a cached value by key, if it exists."""
        try:
            self._file(key).unlink()

        except FileNotFoundError:
            pass


class CacheManager:
    """Derives the installed package info from the filesystem and the catalog.

    The installed records are cached under a single token-invalidated key, and the
    join against the catalog is computed on read.
    """

    _RECORDS_KEY = "installed_records"

    def __init__(
        self,
        cache: Cache,
        catalog: Catalog,
        env: BreweryENV | None = None,
    ) -> None:
        """Initialise with a Cache instance, catalog, and optional environment.

        Args:
            cache: File-based cache to use for FS record cache.
            catalog: A Catalog instance to use for resolving package details.
            env: Optional BreweryENV instance for environment-specific paths.
        """
        self.cache: Cache = cache
        self.catalog: Catalog = catalog
        self.env: BreweryENV | None = env

        log.debug(event="cache_manager_initialised")

    async def installed_records(self) -> list[InstalledRecord]:
        """Return installed records from cache, or scan if not cached.

        Returns:
            A list of InstalledRecord instances for the installed packages.
        """
        cached: Any = self.cache.get(self._RECORDS_KEY)
        if cached is not None:
            return [record_from_cache(d) for d in cached]

        records: list[InstalledRecord] = scan_installed(env=self.env)
        await attach_sizes(records=records)

        self.cache.set(self._RECORDS_KEY, [records_to_cache(r) for r in records])

        return records

    async def installed_packages(
        self, kind: Optional[PackageKind] = None
    ) -> list[Package]:
        """Return merged installed packages, optionally filtered by kind.

        Args:
            kind: Optional PackageKind to filter by.

        Returns:
            A list of Package instances, sorted by kind, then name.
        """
        records: list[InstalledRecord] = await self.installed_records()
        packages: list[Package] = merge(records, self.catalog)

        if kind is not None:
            packages = [p for p in packages if p.kind == kind]

        packages.sort(key=lambda p: (p.kind.value, p.name))

        return packages

    def invalidate(self) -> None:
        """Invalidate FS cache so it is rebuilt on next access."""
        self.cache.delete(self._RECORDS_KEY)
        log.debug(event="installed_records_invalidated")
