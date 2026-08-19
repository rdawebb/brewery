"""Command-family services: the policy layer between the CLI and the data facade.

The layering runs strictly one way: `cli` -> `services` -> {`core`, `providers`}.
Nothing in `core` or `providers` may import from here; `tests/unit/test_layering.py`
enforces that.
"""

from __future__ import annotations
