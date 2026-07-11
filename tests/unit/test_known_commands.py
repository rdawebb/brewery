"""Unit tests for the derived brew-passthrough command set."""

from __future__ import annotations

import pytest
from typer_extensions import ExtendedTyper

from brewery.cli.main import KNOWN_COMMANDS, _derive_known_commands

pytestmark = pytest.mark.unit

# The full set of tokens brewery owns: every command name, sub-app name, and alias
# Kept explicit so a dropped/renamed command or alias is caught as a regression
EXPECTED_KNOWN_COMMANDS = {
    "list", "ls", "l",
    "info", "i", "in",
    "search", "s", "find",
    "install", "add",
    "uninstall", "rm", "del",
    "outdated", "o", "out",
    "upgrade", "u", "up",
    "cleanup", "c", "clean",
    "pin", "p",
    "unpin", "unp",
    "link", "ln",
    "unlink", "ul",
    "daemon", "d",
    "config", "cfg",
}  # fmt: skip


class TestKnownCommands:
    """Tests for the KNOWN_COMMANDS derivation."""

    def test_derived_set_matches_expected(self) -> None:
        """The real app derives exactly the expected command/alias tokens."""
        assert KNOWN_COMMANDS == EXPECTED_KNOWN_COMMANDS

    def test_derivation_covers_commands_groups_and_aliases(self) -> None:
        """Derivation picks up command names, sub-app names, and aliases alike."""
        synthetic = ExtendedTyper()

        @synthetic.command(name="foo", aliases=["f", "fo"])
        def foo() -> None: ...

        @synthetic.command(aliases=["li"])
        def list_it() -> None: ...

        sub = ExtendedTyper()

        @sub.callback()
        def _sub_cb() -> None: ...

        synthetic.add_typer(sub, name="bar", aliases=["b"])

        assert _derive_known_commands(synthetic) == {
            "foo", "f", "fo",       # command + aliases
            "list_it", "li",        # underscore name kept verbatim + alias
            "bar", "b",             # sub-app + alias
        }  # fmt: skip
