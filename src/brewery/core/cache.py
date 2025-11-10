"""A simple file-based cache with expiration."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from rich.console import Console

from brewery.core.config import CACHE_DIR, Brewery
from brewery.core.errors import CacheError, TransientError
from brewery.core.logging import get_logger

log = get_logger(__name__)
console = Console()


class Cache:
    """A simple file-based cache with expiration."""

    def __init__(self, namespace: str):
        self.cache_path = CACHE_DIR / namespace
        self.cache_path.mkdir(parents=True, exist_ok=True)
        log.debug(
            "cache_initialized", 
            namespace=namespace,
            path=str(self.cache_path)
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
        def mtime(p: Path) -> int:
            try:
                return int(p.stat().st_mtime)
            except FileNotFoundError:
                return 0
            
        return f"{mtime(Brewery.cellar)}-{mtime(Brewery.caskroom)}"
    
    def get_or_set(
        self, key: str, ttl: int, loader: Callable[[], Any], allow_stale: bool = False
    ) -> Any:
        """Get a cached value or set it using the loader function.
        
        Args:
            key: The cache key.
            ttl: Time-to-live in seconds.
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
                if (now - data.get("_ts", 0) < ttl) and data.get("_token") == token:
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    age_seconds = now - data["_ts"]
                    log.info(
                        "cache_hit",
                        key=key,
                        namespace=self.cache_path.name,
                        age_seconds=age_seconds,
                        duration_ms=duration_ms
                    )
                else:
                    reason = "expired" if (now - data.get("_ts", 0) >= ttl) else "token_mismatch"
                    log.debug(
                        "cache_invalid",
                        key=key,
                        namespace=self.cache_path.name,
                        reason=reason
                    )
                    if allow_stale:
                        stale_data = data.get("value")

            except json.JSONDecodeError:
                log.warning(
                    "cache_corrupted",
                    key=key,
                    namespace=self.cache_path.name,
                    exc_info=True
                )
            except Exception as e:
                log.error(
                    "cache_read_error",
                    key=key,
                    namespace=self.cache_path.name,
                    exc_info=True
                )
                raise CacheError(
                    "Failed to read cache entry",
                    context={
                        "key": key,
                        "namespace": self.cache_path.name,
                        "error": str(e)
                    }
                )

        log.info(
            "cache_miss",
            key=key,
            namespace=self.cache_path.name
        )

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
                    error=str(e)
                )
                console.print(
                    "⚠️ Using cached data due to temporary error (may be outdated).\n",
                    style="bold yellow"
                )
                return stale_data
            else:
                raise

        try:
            f.write_text(
                json.dumps({
                    "_ts": now,
                    "_token": token,
                    "value": value
                })
            )
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.info(
                "cache_set",
                key=key,
                namespace=self.cache_path.name,
                duration_ms=duration_ms
            )

        except Exception as e:
            log.error(
                "cache_write_error",
                key=key,
                namespace=self.cache_path.name,
                error=str(e),
                exc_info=True
            )
            raise CacheError(
                "Failed to write cache entry",
                context={
                    "key": key,
                    "namespace": self.cache_path.name,
                    "error": str(e)
                }
            ) from e

        return value