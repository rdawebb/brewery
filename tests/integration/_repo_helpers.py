"""Helpers shared by the integration tests that drive a real prefix."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from _layout import full_receipt, write_keg


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
    """Write an installed keg + receipt so the scan derives used_by.

    Args:
        cellar: The cellar directory to write to
        name: The name of the formula
        version: The version of the formula (default: "1.0")
        deps: The dependencies of the formula (default: ())

    Returns:
        The path to the installed keg
    """
    return write_keg(
        cellar,
        name,
        version,
        receipt=full_receipt(
            runtime_dependencies=[{"full_name": d} for d in deps],
        ),
    )


def zip_app(tmp_path, cache, body: str = "#!/bin/sh\n") -> str:
    """Zip a minimal `Foo.app` into the download cache, as if already fetched.

    Args:
        tmp_path: The pytest tmp_path fixture.
        cache: The downloader's cache dir.
        body: The app's `foo` executable, to tell versions apart.

    Returns:
        The zip's sha256, which is its cache key.
    """
    work = Path(tempfile.mkdtemp(dir=tmp_path))
    macos = work / "src" / "Foo.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "foo").write_text(body)
    archive = work / "foo.zip"
    subprocess.run(["ditto", "-c", "-k", str(work / "src"), str(archive)], check=True)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    shutil.copy(archive, cache / sha)

    return sha
