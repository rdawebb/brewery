"""Tests the config and daemon sub-apps, neither of which opens a repository."""

from __future__ import annotations

import pytest

from brewery.cli.commands import daemon as daemon_mod
from brewery.cli.commands.config import _config_status, _fmt_count
from brewery.core.errors import EXIT_USER_ERROR, SysError


class TestFmtCount:
    """Tests _fmt_count, which renders every capped setting in `config show`."""

    @pytest.mark.parametrize(
        ("value", "unit", "expected"),
        [
            pytest.param(None, "day", "unlimited", id="uncapped"),
            pytest.param(0, "day", "0 days", id="zero_is_plural"),
            pytest.param(1, "day", "1 day", id="one_is_singular"),
            pytest.param(2, "day", "2 days", id="many_are_plural"),
            pytest.param(1, "version", "1 version", id="other_unit_singular"),
            pytest.param(5, "version", "5 versions", id="other_unit_plural"),
        ],
    )
    def test_the_count_is_pluralised(self, value, unit, expected) -> None:
        """Test that None reads as unlimited and only one is singular."""
        assert _fmt_count(value, unit) == expected


class TestConfigStatus:
    """Tests _config_status, the four states an on-disk config can be in."""

    def test_a_missing_file_is_fine(self, tmp_path) -> None:
        """Test that no config file is a normal state, not a problem."""
        assert _config_status(tmp_path / "absent.json") == (
            "not found — using defaults",
            True,
        )

    def test_a_valid_object_loads(self, tmp_path) -> None:
        """Test that a well-formed config reports as loaded."""
        path = tmp_path / "config.json"
        path.write_text('{"retention": {"age_days": 30}}')

        assert _config_status(path) == ("loaded", True)

    def test_malformed_json_is_flagged_rather_than_raised(self, tmp_path) -> None:
        """Test that a corrupt config degrades to defaults with a warning.

        Raising here would make every command unusable until the file was
        hand-repaired, which is worse than falling back.
        """
        path = tmp_path / "config.json"
        path.write_text("{not json")

        assert _config_status(path) == ("invalid JSON — using defaults", False)

    def test_valid_json_that_is_not_an_object_is_flagged(self, tmp_path) -> None:
        """Test that a JSON list or scalar is rejected as a config."""
        path = tmp_path / "config.json"
        path.write_text("[1, 2, 3]")

        assert _config_status(path) == ("not a JSON object — using defaults", False)


class TestConfigCommands:
    """Tests the config sub-app's commands."""

    def test_show_renders_the_same_thing(self, run) -> None:
        """Test that `config show` is the explicit form of the default."""
        result = run(["config", "show"])

        assert result.exit_code == 0
        assert "Retention" in result.output

    def test_path_prints_the_config_file_location(self, run) -> None:
        """Test that `config path` emits a bare path for piping into an editor."""
        result = run(["config", "path"])

        assert result.exit_code == 0
        assert result.output.strip().endswith(".json")

    def test_get_prints_one_resolved_value(self, run) -> None:
        """Test that `config get` emits a bare value, defaults applied."""
        result = run(["config", "get", "display.format"])

        assert result.exit_code == 0
        assert result.output.strip()

    def test_get_on_an_unknown_key_exits_user_error(self, run) -> None:
        """Test that a typo'd key is a user error, not a traceback."""
        result = run(["config", "get", "no.such.key"])

        assert result.exit_code == EXIT_USER_ERROR

    def test_set_writes_the_value_and_confirms(self, run) -> None:
        """Test that `config set` reports what it changed."""
        result = run(["config", "set", "retention.age_days", "45"])

        assert result.exit_code == 0
        assert "retention.age_days" in result.output
        assert "45" in result.output

    def test_set_accepts_unlimited_to_remove_a_cap(self, run) -> None:
        """Test that "unlimited" is the documented way to disable a cap."""
        result = run(["config", "set", "retention.max_versions", "unlimited"])

        assert result.exit_code == 0
        assert "unlimited" in result.output

    def test_set_on_an_unknown_key_exits_user_error(self, run) -> None:
        """Test that setting a key that does not exist fails cleanly."""
        result = run(["config", "set", "no.such.key", "1"])

        assert result.exit_code == EXIT_USER_ERROR


class TestDaemonCommands:
    """Tests the daemon sub-app's four commands."""

    @pytest.fixture
    def service(self, monkeypatch):
        """Stub the daemon layer and record which operations were asked for.

        Args:
            monkeypatch: The pytest monkeypatch fixture.

        Returns:
            A callable configuring warnings/running state and returning the log.
        """

        def _arrange(warnings=(), running=False, start_raises=None):
            calls: list[str] = []

            def _start():
                calls.append("start")
                if start_raises is not None:
                    raise start_raises

                return list(warnings)

            monkeypatch.setattr(daemon_mod.daemon, "start", _start)
            monkeypatch.setattr(daemon_mod.daemon, "stop", lambda: calls.append("stop"))
            monkeypatch.setattr(daemon_mod.daemon, "is_running", lambda: running)

            return calls

        return _arrange

    def test_start_installs_and_confirms(self, run, service) -> None:
        """Test that `daemon start` activates the service and says so."""
        calls = service()

        result = run(["daemon", "start"])

        assert result.exit_code == 0
        assert calls == ["start"]
        assert "Daemon installed and loaded" in result.output

    def test_start_surfaces_the_launchd_advisories(self, run, service) -> None:
        """Test that warnings from the platform layer reach the user."""
        service(warnings=["Full Disk Access may be required"])

        result = run(["daemon", "start"])

        assert "Full Disk Access may be required" in result.output

    def test_stop_removes_and_confirms(self, run, service) -> None:
        """Test that `daemon stop` deactivates the service and says so."""
        calls = service()

        result = run(["daemon", "stop"])

        assert result.exit_code == 0
        assert calls == ["stop"]
        assert "Daemon removed" in result.output

    def test_restart_stops_before_starting(self, run, service) -> None:
        """Test that restart is a stop then a start, in that order.

        Starting first would leave two agents registered under one label.
        """
        calls = service()

        result = run(["daemon", "restart"])

        assert result.exit_code == 0
        assert calls == ["stop", "start"]
        assert "Daemon restarted" in result.output

    def test_status_reports_an_active_daemon(self, run, service) -> None:
        """Test that a running daemon is reported as active."""
        service(running=True)

        result = run(["daemon", "status"])

        assert result.exit_code == 0
        assert "is active" in result.output
        assert "daemon stop" in result.output

    def test_status_reports_an_inactive_daemon(self, run, service) -> None:
        """Test that a stopped daemon is reported with the command to start it."""
        service(running=False)

        result = run(["daemon", "status"])

        assert result.exit_code == 0
        assert "not active" in result.output
        assert "daemon start" in result.output

    def test_a_failure_to_start_becomes_an_exit_code(self, run, service) -> None:
        """Test that a platform error maps through handle_error, not a traceback."""
        service(start_raises=SysError("launchctl not found"))

        result = run(["daemon", "start"])

        assert result.exit_code != 0
        assert "launchctl not found" in result.output
