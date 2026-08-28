"""Shared facts about the CLI's repository seam."""

from __future__ import annotations

# cli/commands/ modules that legitimately never open a repository
REPO_FREE_MODULES = frozenset({"config", "daemon"})
