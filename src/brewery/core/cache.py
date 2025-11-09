"""A simple cache system for storing and retrieving data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Cache:
    """A simple in-memory cache system (with optional file persistence)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}

        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                self.data = {}

    def get(self, key: str) -> Any | None:
        """Retrieve a value from the cache by key.
        
        Args:
            key (str): The key to retrieve.
        """
        return self.data.get(key)
    
    def set(self, key: str, value: Any) -> None:
        """Set a value in the cache by key.

        Args:
            key (str): The key to set.
            value (Any): The value to set.
        """
        self.data[key] = value
        self.path.write_text(json.dumps(self.data))