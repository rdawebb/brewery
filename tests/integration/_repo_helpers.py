"""Helpers shared by the integration tests that drive a real prefix."""

from __future__ import annotations

from pathlib import Path


class _NullSink:
    """Stands in for StreamRelocator where the keg is already staged."""

    def finish(self, keg: Path) -> None:
        """Do nothing, as there is nothing staged to relocate.

        Args:
            keg: The keg directory, ignored.
        """


def _provider_calls(mock_brew, subcommand: str) -> list[tuple[str, ...]]:
    """Filter the mock_brew call log to brew invocations of a given subcommand.

    Args:
        mock_brew: The mock brew call log.
        subcommand: The subcommand to filter by.

    Returns:
        A list of tuples representing the filtered brew calls.
    """
    return [
        c for c in mock_brew if len(c) >= 2 and c[0] == "brew" and c[1] == subcommand
    ]


def _add_alias(catalog, alias: str, name: str) -> None:
    """Register an alias -> canonical name mapping in the catalog.

    Args:
        catalog: The catalog to write to.
        alias: The alias a user might type.
        name: The canonical formula name it resolves to.
    """
    with catalog._conn:
        catalog._conn.execute(
            "INSERT OR REPLACE INTO alias (alias, name) VALUES (?, ?)", (alias, name)
        )


def _install_formula(cellar, name, version="1.0", deps=()) -> Path:
    """Write a minimal installed keg + receipt so the scan derives used_by.

    Args:
        cellar: The cellar directory to write to
        name: The name of the formula
        version: The version of the formula (default: "1.0")
        deps: The dependencies of the formula (default: ())

    Returns:
        The path to the installed keg
    """
    import orjson

    keg = cellar / name / version
    keg.mkdir(parents=True)
    (keg / "INSTALL_RECEIPT.json").write_bytes(
        orjson.dumps(
            {
                "source": {"tap": "homebrew/core"},
                "runtime_dependencies": [{"full_name": d} for d in deps],
            }
        )
    )

    return keg
