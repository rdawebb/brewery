"""Unit tests for the --version flag on the root callback."""

from __future__ import annotations

from importlib.metadata import version

import pytest
from typer.testing import CliRunner

import brewery
from brewery.cli.context import app

pytestmark = pytest.mark.unit

runner = CliRunner()


class TestVersionFlag:
    """Tests the behaviour of `brewery --version`."""

    def test_prints_version_and_exits_zero(self) -> None:
        """Test the flag reports the version on its own, with no subcommand, and exits 0."""
        result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0
        assert result.stdout.strip() == f"Brewery {brewery.__version__}"

    def test_reports_the_installed_distribution_version(self) -> None:
        """Test the printed version comes from package metadata, not a hardcoded literal."""
        result = runner.invoke(app, ["--version"])

        assert version("brewery") in result.stdout

    def test_absent_flag_prints_nothing(self) -> None:
        """Test the default (False) path is silent: no version banner on other invocations."""
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        assert f"Brewery {brewery.__version__}" not in result.stdout

    def test_short_circuits_before_the_callback_body(self) -> None:
        """Test the eager callback exits during parsing, so setup() never configures logging."""
        configured = False

        def _configure_logging(**kwargs) -> None:
            """Configure logging during callback execution."""
            nonlocal configured
            configured = True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("brewery.cli.context.configure_logging", _configure_logging)
            result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0
        assert not configured
