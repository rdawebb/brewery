"""Unit tests for main() routing: brewery command vs brew passthrough."""

from __future__ import annotations

import pytest

from brewery.cli import main as main_mod


class _Recorder:
    """Records whether it was called and with what arguments."""

    def __init__(self) -> None:
        """Initialise an empty call list."""
        self.calls: list[tuple] = []

    def __call__(self, *args) -> int:
        """Record the call arguments and return 0."""
        self.calls.append(args)
        return 0

    @property
    def called(self) -> bool:
        """Return whether the recorder has recorded any calls."""
        return bool(self.calls)


@pytest.fixture
def routes(monkeypatch):
    """Drive main(argv) with app() and _brew_passthrough stubbed; report the path.

    Returns a function mapping argv -> "app" (dispatched to ExtendedTyper) or
    "brew" (forwarded to passthrough).
    """
    app_stub = _Recorder()
    passthrough = _Recorder()
    monkeypatch.setattr(main_mod, "app", app_stub)
    monkeypatch.setattr(main_mod, "_brew_passthrough", passthrough)

    def _route(argv: list[str]) -> str:
        """Route the given argv to the app or brew passthrough stub.

        Args:
            argv: The command-line arguments to route.

        Returns:
            "app" if the command is dispatched to the app stub, "brew" if forwarded to passthrough.
        """
        app_stub.calls.clear()
        passthrough.calls.clear()
        try:
            main_mod.main(argv)

        except SystemExit:
            pass  # Passthrough path exits with the (stubbed) return code

        if passthrough.called:
            assert not app_stub.called
            assert passthrough.calls[0][0] == argv  # Forwarded verbatim
            return "brew"

        assert app_stub.called
        return "app"

    return _route


class TestRouting:
    """Routing/passthrough table for main()."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(["install", "wget"], "app", id="command"),
            pytest.param(["ls"], "app", id="alias"),
            pytest.param(["config", "show"], "app", id="subapp"),
            pytest.param(["daemon"], "app", id="subapp_bare"),
            # Formerly passed through to brew; native since pin/link landed
            pytest.param(["pin", "wget"], "app", id="pin"),
            pytest.param(["unpin", "wget"], "app", id="unpin"),
            pytest.param(["link", "wget"], "app", id="link"),
            pytest.param(["ln", "wget"], "app", id="link_alias"),
            pytest.param(["unlink", "wget"], "app", id="unlink"),
            pytest.param(["doctor"], "brew", id="unknown_command"),
            pytest.param(["bogus", "x"], "brew", id="unknown_with_args"),
            pytest.param(["--help"], "app", id="flag_first"),
            pytest.param(["--version"], "app", id="version_flag"),
            pytest.param([], "app", id="empty"),
        ],
    )
    def test_routes(self, routes, argv, expected) -> None:
        """Test known commands/aliases dispatch to ExtendedTyper; unknown non-flags go to brew."""
        assert routes(argv) == expected
