"""Decide which casks brewery can install natively, and which fall back to brew."""

from __future__ import annotations

import operator
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from brewery.core.catalog import CaskRow
from brewery.core.host import Platform

Route = Literal["native", "brew"]

# Artifact stanzas brewery can stage itself (`target` annotates an `app`/`binary`)
NATIVE_ARTIFACTS = frozenset(
    {
        "app",
        "binary",
        "font",
        "suite",
        "qlplugin",
        "artifact",
        "manpage",
        "bash_completion",
        "zsh_completion",
        "fish_completion",
        "uninstall",
        "zap",
        "target",
    }
)

# `uninstall`/`zap` directives `cask_directives` runs, and its `on_upgrade` option
NATIVE_UNINSTALL = frozenset(
    {"quit", "delete", "launchctl", "signal", "trash", "rmdir", "on_upgrade"}
)

# `url` options brew sends as request headers
_HEADER_SPECS = frozenset({"user_agent", "referer", "cookies"})

# `HOMEBREW_USER_AGENT_FAKE_SAFARI`, sent for `user_agent: :fake` and `:browser`
_FAKE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/27.0 Safari/605.1.15"
)
_FAKE_AGENTS = frozenset({":fake", ":browser"})

_ARCH_TYPES = {"arm": "arm64", "intel": "amd64"}

_COMPARE: dict[str, Callable[[int, int], bool]] = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}


@dataclass(frozen=True)
class CaskOpts:
    """The parts of `HOMEBREW_CASK_OPTS` a native install has to honour.

    Attributes:
        dirs: `--<name>dir=` overrides, keyed as brew's config keys them.
        binaries: False for `--no-binaries`.
        require_sha: True for `--require-sha`.
        languages: `--language=` values, preferred over the system's.
    """

    dirs: dict[str, Path] = field(default_factory=dict)
    binaries: bool = True
    require_sha: bool = False
    languages: tuple[str, ...] = ()


def read_cask_opts(raw: str) -> CaskOpts:
    """Parse `HOMEBREW_CASK_OPTS` the way brew's `Cask::Config#env` does.

    Args:
        raw: The variable's value.

    Returns:
        The parsed options; unknown switches are ignored.
    """
    dirs: dict[str, Path] = {}
    binaries = True
    languages: tuple[str, ...] = ()
    args = shlex.split(raw)
    for arg in args:
        flag, eq, value = arg.partition("=")
        key = flag.removeprefix("--").replace("-", "_")

        if eq and key == "language":
            languages = tuple(value.split(","))

        elif eq:
            dirs[key] = Path(os.path.abspath(os.path.expanduser(value)))

        elif arg in ("--binaries", "--no-binaries"):
            binaries = arg == "--binaries"  # The last one wins

    return CaskOpts(dirs, binaries, "--require-sha" in args, languages)


def classify(row: CaskRow, plat: Platform | None) -> tuple[Route, str]:
    """Route a cask to the native installer or to brew, with the reason.

    Args:
        row: The cask's catalog row.
        plat: The host platform, from `current_platform()`.

    Returns:
        Tuple of (route, reason). The reason is empty for a native cask.
    """
    if plat is None or plat.os != "macos" or plat.macos_major is None:
        return "brew", "native casks need macOS"

    if row.container:
        return "brew", "`container` unpacking"

    if reason := _url_reason(row.url_specs or {}):
        return "brew", reason

    if row.language_variations:
        return "brew", "`language` variant"  # One `localise` couldn't apply

    if reason := _artifact_reason(row.artifacts or [], ("uninstall",)):
        return "brew", reason

    return _check_depends(row.depends_on or {}, plat.macos_major, plat.arch)


def url_headers(specs: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """The request headers a cask's `url` options ask for.

    Args:
        specs: The cask's decoded `url_specs`, or None.

    Returns:
        (name, value) header pairs; empty when the cask asks for none.
    """
    specs = specs or {}
    headers: list[tuple[str, str]] = []

    if agent := specs.get("user_agent"):
        headers.append(
            ("User-Agent", _FAKE_USER_AGENT if agent in _FAKE_AGENTS else agent)
        )

    if referer := specs.get("referer"):
        headers.append(("Referer", referer))

    if cookies := specs.get("cookies"):
        headers.append(("Cookie", ";".join(f"{k}={v}" for k, v in cookies.items())))

    return tuple(headers)


def _url_reason(specs: dict[str, Any]) -> str:
    """Why a cask's `url` options need brew to fetch it.

    Args:
        specs: The cask's decoded `url_specs`.

    Returns:
        The reason, or an empty string when `url_headers` covers them.
    """
    # git checkout or POST: only brew's strategies fetch
    if foreign := sorted(specs.keys() - _HEADER_SPECS):
        return f"`url {foreign[0]}:`"

    # `:curl` sends curl's own agent, which brewery doesn't have
    agent = specs.get("user_agent")
    if isinstance(agent, str) and agent.startswith(":") and agent not in _FAKE_AGENTS:
        return f"`url user_agent: {agent}`"

    return ""


def classify_uninstall(
    receipt: dict[str, Any] | None, plat: Platform | None, *, zap: bool
) -> str:
    """Why an installed cask's uninstall goes to brew, judged from its receipt.

    brew uninstalls from the receipt's `uninstall_artifacts`, not the current cask.

    Args:
        receipt: The cask's `INSTALL_RECEIPT.json`, or None if it has none.
        plat: The host platform, from `current_platform()`.
        zap: Whether the `zap` directives run too.

    Returns:
        The reason, or an empty string for a native uninstall.
    """
    if plat is None or plat.os != "macos":
        return "native casks need macOS"

    artifacts = (receipt or {}).get("uninstall_artifacts")
    if not isinstance(artifacts, list) or (receipt or {}).get(
        "uninstall_flight_blocks"
    ):
        return "no uninstall receipt"

    return _artifact_reason(artifacts, ("uninstall", "zap") if zap else ("uninstall",))


def _artifact_reason(artifacts: list[Any], directives: tuple[str, ...]) -> str:
    """The first artifact, or directive of those stanzas, brewery can't handle.

    Args:
        artifacts: The cask's artifacts, or a receipt's `uninstall_artifacts`.
        directives: The stanzas whose directives must be native.

    Returns:
        The reason, or an empty string.
    """
    for stanza in artifacts:
        if not isinstance(stanza, dict):
            return "unrecognised artifact"

        foreign = sorted(stanza.keys() - NATIVE_ARTIFACTS)
        if foreign:
            return f"`{foreign[0]}` artifact"

        for key in directives:
            stanzas = stanza.get(key, [])
            if not isinstance(stanzas, list) or not all(
                isinstance(d, dict) for d in stanzas
            ):
                return f"unrecognised `{key}` directive"

            for directive in stanzas:
                foreign = sorted(directive.keys() - NATIVE_UNINSTALL)
                if foreign:
                    return f"`{key} {foreign[0]}`"

    return ""


def _check_depends(
    depends_on: dict[str, Any], macos_major: int, arch: str
) -> tuple[Route, str]:
    """Hand casks this host can't run to brew, which refuses with its own message.

    `formula` and `cask` dependencies are resolved by the installer.

    Args:
        depends_on: The cask's decoded `depends_on`.
        macos_major: The host's macOS major version.
        arch: The host's arch token.

    Returns:
        Tuple of (route, reason).
    """
    for key in ("macos", "maximum_macos"):
        for op, versions in (depends_on.get(key) or {}).items():
            # The host is known only to its major version, so compare majors
            majors = [int(v.split(".")[0]) for v in versions]
            ok = (
                macos_major in majors
                if op == "=="
                else _COMPARE[op](macos_major, majors[0])
            )

            if not ok:
                return "brew", f"needs macOS {op} {', '.join(versions)}"

    archs = {_ARCH_TYPES.get(a.get("type")) for a in depends_on.get("arch") or []}
    if archs and arch not in archs:
        return "brew", f"needs {'/'.join(sorted(a for a in archs if a))}"

    return "native", ""
