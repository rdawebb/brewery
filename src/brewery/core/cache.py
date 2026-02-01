"""A simple file-based cache with expiration."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from rich.console import Console

from brewery.core.config import CACHE_DIR, get_brewery_env
from brewery.core.errors import CacheError, TransientError
from brewery.core.logging import get_logger

log = get_logger(__name__)
console = Console()

_cached_token = None
_token_timestamp = 0


class Cache:
    """A simple file-based cache with expiration."""

    def __init__(self, namespace: str):
        self.cache_path = CACHE_DIR / namespace
        self.cache_path.mkdir(parents=True, exist_ok=True)
        self._cached_token = None
        self._token_timestamp = 0
        log.debug("cache_initialised", namespace=namespace, path=str(self.cache_path))

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
        start_time = time.perf_counter()
        now = time.time()
        if _cached_token and (now - _token_timestamp) < 1:
            return _cached_token

        print(
            f"Before get_brewery_env: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )
        brewery = get_brewery_env()
        print(
            f"After get_brewery_env: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )

        def mtime(p: Path) -> int:
            try:
                return int(p.stat().st_mtime)
            except FileNotFoundError:
                return 0

        print(f"Before stat cellar: {(time.perf_counter() - start_time) * 1000:.2f} ms")
        cellar_mtime = mtime(brewery.cellar)
        print(f"After stat cellar: {(time.perf_counter() - start_time) * 1000:.2f} ms")

        print(
            f"Before stat caskroom: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )
        caskroom_mtime = mtime(brewery.caskroom)
        print(
            f"After stat caskroom: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )

        _cached_token = f"{cellar_mtime}-{caskroom_mtime}"
        _token_timestamp = now

        print(
            f"_update_token total time: {(time.perf_counter() - start_time) * 1000:.2f} ms"
        )
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
        f = self._file(key)
        now = int(time.time())
        token = self._update_token()
        start = time.perf_counter()
        stale_data = None

        if f.exists():
            try:
                data = json.loads(f.read_text())
                ttl_valid = ttl is None or (now - data.get("_ts", 0) < ttl)
                if ttl_valid and data.get("_token") == token:
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    age_seconds = now - data["_ts"]
                    log.info(
                        "cache_hit",
                        key=key,
                        namespace=self.cache_path.name,
                        age_seconds=age_seconds,
                        duration_ms=duration_ms,
                    )
                    return data.get("value")
                else:
                    reason = (
                        "expired"
                        if (now - data.get("_ts", 0) >= ttl)
                        else "token_mismatch"
                    )
                    log.debug(
                        "cache_invalid",
                        key=key,
                        namespace=self.cache_path.name,
                        reason=reason,
                    )
                    if allow_stale:
                        stale_data = data.get("value")

            except json.JSONDecodeError:
                log.warning(
                    "cache_corrupted",
                    key=key,
                    namespace=self.cache_path.name,
                    exc_info=True,
                )
            except Exception as e:
                log.error(
                    "cache_read_error",
                    key=key,
                    namespace=self.cache_path.name,
                    exc_info=True,
                )
                raise CacheError(
                    key=key,
                    namespace=self.cache_path.name,
                    operation="read",
                ) from e

        log.info("cache_miss", key=key, namespace=self.cache_path.name)

        try:
            value = loader()
        except TransientError as e:
            if allow_stale and stale_data is not None:
                age_seconds = now - data.get("_ts", now)
                log.warning(
                    "cache_fallback_stale",
                    key=key,
                    namespace=self.cache_path.name,
                    age_seconds=age_seconds,
                    error=str(e),
                )
                console.print(
                    "⚠️ Using cached data due to temporary error (may be outdated).\n",
                    style="bold yellow",
                )
                return stale_data
            else:
                raise

        try:
            f.write_text(json.dumps({"_ts": now, "_token": token, "value": value}))
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                "cache_set",
                key=key,
                namespace=self.cache_path.name,
                duration_ms=duration_ms,
            )

        except Exception as e:
            log.error(
                "cache_write_error",
                key=key,
                namespace=self.cache_path.name,
                error=str(e),
                exc_info=True,
            )
            raise CacheError(
                key=key, namespace=self.cache_path.name, operation="write", path=str(f)
            ) from e

        return value

    def get(self, key: str) -> Optional[Any]:
        """Get a cached value by key.

        Args:
            key: The cache key.

        Returns:
            The cached value, or None if not found.
        """
        start = time.perf_counter()
        f = self._file(key)
        print(f"_file(): {(time.perf_counter() - start) * 1000:.2f} ms")
        if not f.exists():
            return None
        print(f"f.exists(): {(time.perf_counter() - start) * 1000:.2f} ms")

        try:
            data = json.loads(f.read_text())
            print(f"JSON loads: {(time.perf_counter() - start) * 1000:.2f} ms")
            token = self._update_token()
            print(f"_update_token(): {(time.perf_counter() - start) * 1000:.2f} ms")
            if token == data.get("_token"):
                log.info("cache_hit", key=key, namespace=self.cache_path.name)
                return data.get("value")
            else:
                log.debug("cache_invalid", key=key, namespace=self.cache_path.name)
                return None

        except json.JSONDecodeError:
            log.warning(
                "cache_corrupted",
                key=key,
                namespace=self.cache_path.name,
                exc_info=True,
            )
        except Exception as e:
            log.error(
                "cache_read_error",
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
        f = self._file(key)
        now = int(time.time())
        token = self._update_token()
        start = time.perf_counter()

        try:
            f.write_text(json.dumps({"_ts": now, "_token": token, "value": value}))
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                "cache_set",
                key=key,
                namespace=self.cache_path.name,
                duration_ms=duration_ms,
            )

        except Exception as e:
            log.error(
                "cache_write_error",
                key=key,
                namespace=self.cache_path.name,
                error=str(e),
                exc_info=True,
            )
            raise CacheError(
                key=key, namespace=self.cache_path.name, operation="write", path=str(f)
            ) from e
