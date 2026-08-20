"""Command-family services: the policy layer between the CLI and the data facade.

The layering runs strictly one way: `cli` -> `services` -> {`core`, `providers`},
enforced by import-linter contracts.
"""

from __future__ import annotations
