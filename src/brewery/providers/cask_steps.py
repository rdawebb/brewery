"""Run a cask's `*_steps` stanzas.

In-process, without `sandbox-exec`: the file steps are this module's own code, acting
only on the paths they name, and `run` steps are trusted as an `installer script` is.
"""

# This file contains code derived from Homebrew (https://github.com/Homebrew/brew)
# Copyright (c) 2009-present, Homebrew contributors
# Licensed under BSD 2-Clause License (see LICENSE-HOMEBREW)
#
# Portions of this module reimplement Homebrew's cask install steps.

from __future__ import annotations

import contextlib
import getpass
import glob
import hashlib
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from brewery.core.errors import CellarError
from brewery.core.logging import BreweryLogger, get_logger
from brewery.core.sudo import run_sudo
from brewery.core.version import _tokens
from brewery.providers.cask_directives import (
    _braces,
    _sudo_each,
    remove,
    run_script,
    undeletable,
)

log: BreweryLogger = get_logger(name=__name__)

STAGES = frozenset(
    {
        "preflight_steps",
        "postflight_steps",
        "uninstall_preflight_steps",
        "uninstall_postflight_steps",
    }
)

# The stages that run when installing
INSTALLING = ("preflight_steps", "postflight_steps")

# The keys each step type takes, besides `type` and `guards`; any other goes to brew
_KEYS: Mapping[str, frozenset[str]] = {
    "mkdir_p": frozenset({"path"}),
    "touch": frozenset({"path"}),
    "write": frozenset({"path", "content", "overwrite"}),
    "move": frozenset({"source", "target", "overwrite", "force", "source_glob"}),
    "move_contents": frozenset({"source", "target"}),
    "copy": frozenset({"source", "target", "recursive", "overwrite", "source_glob"}),
    "symlink": frozenset(
        {"source", "target", "force", "uninstall", "source_glob", "sudo"}
    ),
    "remove": frozenset(
        {"paths", "recursive", "sudo", "symlink_target_contains", "content_contains"}
    ),
    "inreplace": frozenset(
        {
            "path",
            "before",
            "after",
            "regexp",
            "regexp_options",
            "skip_audit",
            "first_only",
        }
    ),
    "set_permissions": frozenset({"paths", "permissions", "non_recursive"}),
    "set_ownership": frozenset({"paths", "user", "group", "non_recursive"}),
    "terminate_process": frozenset(
        {
            "name",
            "match",
            "sudo",
            "attempts",
            "must_succeed",
            "notices",
            "failure_message",
        }
    ),
    # No sandbox, so `network_access` has nothing to allow
    "run": frozenset({"command", "args", "env", "sudo", "network_access"}),
    "delete_keychain_certificate": frozenset({"name", "matching_certificate"}),
}

# Path spec bases: `Context.roots`' keys (the dirs are `DEFAULT_DIRS`'), and the
# three resolved without a root
_BASES = frozenset(
    {
        "staged_path",
        "caskroom_path",
        "temp",
        "home",
        "homebrew_prefix",
        "appdir",
        "fontdir",
        "qlplugindir",
        "absolute",
        "relative",
        "search_path",
    }
)

# `{{…}}` tokens: `Context.values`' keys; leaves any other verbatim
_TOKENS = frozenset(
    {
        "HOMEBREW_PREFIX",
        "HOMEBREW_CELLAR",
        "token",
        "user",
        "version",
        "version.major",
        "version.major_minor",
        "staged_path",
        "appdir",
        "caskroom_path",
        "temp",
    }
)

_TOKEN = re.compile(r"\{\{([A-Za-z_][\w.]*)\}\}")
_GLOB = re.compile(r"[?*\[{]")

_REGEXP_FLAGS = {1: re.IGNORECASE, 2: re.VERBOSE, 4: re.DOTALL}

_APP_MANAGEMENT = (
    "macOS prevents modifying apps without App Management permissions, even with "
    "sudo. Approve the prompt if one was shown, or add your terminal under System "
    "Settings → Privacy & Security → App Management, then try again."
)


@dataclass(frozen=True)
class Context:
    """What a cask's steps resolve paths and templates against.

    Attributes:
        roots: The dirs a path spec's `base` names.
        values: The `{{…}}` template values.
        prefix: The Homebrew prefix, whose `bin` and `sbin` lead a `run` step's PATH.
    """

    roots: Mapping[str, Path]
    values: Mapping[str, str]
    prefix: Path


def context(
    token: str,
    version: str,
    staged: Path,
    temp: Path,
    prefix: Path,
    cellar: Path,
    dirs: Mapping[str, Path],
) -> Context:
    """Build the context a cask's steps run in.

    Args:
        token: The cask's token.
        version: The version being installed or removed.
        staged: Its version dir in the Caskroom.
        temp: A scratch dir for `{{temp}}`, which an upgrade shares across versions.
        prefix: The Homebrew prefix.
        cellar: The Homebrew Cellar.
        dirs: The dirs moved artifacts go to, `appdir` among them.

    Returns:
        The context.
    """
    roots = {
        "staged_path": staged,
        "caskroom_path": staged.parent,
        "temp": temp,
        "home": Path.home(),
        "homebrew_prefix": prefix,
        **dirs,
    }

    parts = [str(t.value) for t in _tokens(version)]

    values = {
        "HOMEBREW_PREFIX": str(prefix),
        "HOMEBREW_CELLAR": str(cellar),
        "token": token,
        "user": os.environ.get("USER") or getpass.getuser(),
        "version": version,
        "version.major": parts[0] if parts else "",
        "version.major_minor": ".".join(parts[:2]),
        **{
            k: str(roots[k]) for k in ("staged_path", "appdir", "caskroom_path", "temp")
        },
    }

    return Context(roots, values, prefix)


def steps(stanza: Mapping[str, Any], stages: Iterable[str]) -> Iterator[dict[str, Any]]:
    """The steps of a stanza's stages, skipping any malformed block.

    Args:
        stanza: One artifact stanza.
        stages: The stage keys to read.

    Yields:
        Each step dict, in order.
    """
    for key in stages:
        for block in stanza.get(key) or []:
            if isinstance(block, dict) and isinstance(block.get("steps"), list):
                yield from (s for s in block["steps"] if isinstance(s, dict))


def needs_sudo(step: Mapping[str, Any]) -> bool:
    """Whether a step always runs through sudo.

    Args:
        step: The step.

    Returns:
        True for `sudo: true` and a keychain removal; `if_needed` and
        `set_ownership` ask only if they turn out to need it.
    """
    return step.get("sudo") is True or step.get("type") == "delete_keychain_certificate"


def unsupported(artifacts: Iterable[Any]) -> str:
    """The first step this interpreter can't run.

    Args:
        artifacts: A cask's artifacts, or a receipt's `uninstall_artifacts`.

    Returns:
        The reason, or an empty string.
    """
    for stanza in artifacts:
        if not isinstance(stanza, dict):
            continue  # `_artifact_reason` reports it

        for key in STAGES & stanza.keys():
            blocks = stanza[key]
            if not isinstance(blocks, list) or not all(
                isinstance(b, dict)
                and isinstance(b.get("steps"), list)
                and all(isinstance(s, dict) for s in b["steps"])
                for b in blocks
            ):
                return f"unrecognised `{key}`"

            for step in steps(stanza, (key,)):
                if reason := _step_reason(step):
                    return f"`{key}` {reason}"

    return ""


def run_steps(block: Iterable[Mapping[str, Any]], ctx: Context) -> None:
    """Run one block of steps when installing.

    A guard is checked once per block, so steps sharing it see the same answer
    even after an earlier one changes what it checks.

    Args:
        block: The block's steps.
        ctx: The context they run in.

    Raises:
        CellarError: If a step fails.
    """
    seen: dict[frozenset[tuple[str, Any]], bool] = {}
    for step in block:
        kind = str(step.get("type"))
        try:
            if all(_guard(g, ctx, seen) for g in step.get("guards") or []):
                _RUNNERS[kind](step, ctx)

        except CellarError:
            raise

        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            re.error,
            shutil.Error,
        ) as exc:
            raise CellarError(f"`{kind}` step failed: {exc}") from exc


def _step_reason(step: Mapping[str, Any]) -> str:
    """Why one step goes to brew.

    Args:
        step: The step.

    Returns:
        The reason, or an empty string.
    """
    kind = str(step.get("type"))
    if kind not in _KEYS:
        return f"step `{kind}`"

    if extra := sorted(step.keys() - _KEYS[kind] - {"type", "guards"}):
        return f"`{kind}` step's `{extra[0]}`"

    # Ruby's `\1` backreferences, which a literal replacement would write out
    if kind == "inreplace" and "\\" in str(step.get("after", "")):
        return "`inreplace` backreference"

    for guard in step.get("guards") or []:
        if not isinstance(guard, dict) or guard.get("condition") not in (
            "if_exists",
            "unless_exists",
            "on",
        ):
            return "guard"

    for spec in _specs(step):
        if spec.get("base", "absolute") not in _BASES:
            return f"base `{spec['base']}`"

    for value in _strings(step):
        for token in _TOKEN.findall(value):
            if token not in _TOKENS:
                return f"template `{{{{{token}}}}}`"

    return ""


def _specs(value: Any) -> Iterator[dict[str, Any]]:
    """Every path spec in a step: a dict with a `path`.

    Args:
        value: The step, or a value in it.

    Yields:
        Each path spec.
    """
    if isinstance(value, dict):
        if isinstance(value.get("path"), str):
            yield value

        for v in value.values():
            yield from _specs(v)

    elif isinstance(value, list):
        for v in value:
            yield from _specs(v)


def _strings(value: Any) -> Iterator[str]:
    """Every string in a step.

    Args:
        value: The step, or a value in it.

    Yields:
        Each string.
    """
    if isinstance(value, str):
        yield value

    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)

    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _expand(value: str, ctx: Context) -> str:
    """Fill in a string's `{{…}}` tokens, leaving unknown ones verbatim.

    Args:
        value: The string.
        ctx: The context.

    Returns:
        The expanded string.
    """
    return _TOKEN.sub(lambda m: ctx.values.get(m[1], m[0]), value)


def _path(spec: Mapping[str, Any], ctx: Context) -> Path:
    """Resolve a path spec.

    Args:
        spec: The `{path, base}` spec.
        ctx: The context.

    Returns:
        The path; absolute unless `base` is `relative`.
    """
    raw = _expand(spec["path"], ctx)
    base = spec.get("base", "absolute")
    if base == "relative":
        return Path(raw)

    if base != "absolute":
        raw = os.path.join(ctx.roots[base], raw)  # An absolute path replaces the root

    return Path(os.path.abspath(os.path.expanduser(raw)))


def _matches(spec: Mapping[str, Any], ctx: Context) -> list[Path]:
    """A path spec's glob matches, or its path if it isn't a glob.

    Args:
        spec: The `{path, base}` spec; `search_path` looks in each `PATH` dir.
        ctx: The context.

    Returns:
        The paths.
    """
    if spec.get("base") == "search_path":
        name = _expand(spec["path"], ctx)
        dirs = os.environ.get("PATH", "").split(os.pathsep)
        candidates = [os.path.join(d, name) for d in dirs if d]

    else:
        candidates = [str(_path(spec, ctx))]

    found: list[Path] = []
    for c in candidates:
        if not _GLOB.search(c):
            found.append(Path(c))
            continue

        found += [Path(m) for p in _braces(c) for m in sorted(glob.glob(p))]

    return found


def _existing(step: Mapping[str, Any], ctx: Context) -> list[Path]:
    """The paths a step's `paths` match that exist.

    Args:
        step: The step.
        ctx: The context.

    Returns:
        The paths.
    """
    return [p for s in step["paths"] for p in _matches(s, ctx) if p.exists()]


def _guard(
    guard: Mapping[str, Any], ctx: Context, seen: dict[frozenset[tuple[str, Any]], bool]
) -> bool:
    """Whether a guard passes, checked once per block.

    Args:
        guard: The guard.
        ctx: The context.
        seen: The block's results so far, keyed by guard.

    Returns:
        True if the step should run.
    """
    key = frozenset(guard.items())
    if key not in seen:
        if guard["condition"] == "on":
            seen[key] = guard.get("value") == "macos"  # Native casks are macOS-only

        else:
            exists = any(p.exists() for p in _matches(guard, ctx))
            seen[key] = exists == (guard["condition"] == "if_exists")

    return seen[key]


def _source(step: Mapping[str, Any], ctx: Context) -> Path:
    """A `move` or `copy` step's source; a `source_glob` must match exactly one path.

    Args:
        step: The step.
        ctx: The context.

    Returns:
        The source path.

    Raises:
        CellarError: If a glob matches no path or several.
    """
    spec = step["source"]
    if step.get("source_glob") is not True:
        return _path(spec, ctx)

    found = {p for p in _matches(spec, ctx) if p.exists() or p.is_symlink()}
    if len(found) != 1:
        raise CellarError(
            f"`{step['type']}` source must match one path: {spec['path']}"
        )

    return found.pop()


def _dest(source: Path, target: Path) -> Path:
    """Where a move or copy ends up: inside `target` if it's a dir.

    Args:
        source: The source.
        target: The target.

    Returns:
        The destination.
    """
    return target / source.name if target.is_dir() else target


def _mkdir_p(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `mkdir_p` step.

    Args:
        step: The step.
        ctx: The context.

    """
    _path(step["path"], ctx).mkdir(parents=True, exist_ok=True)


def _touch(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `touch` step.

    Args:
        step: The step.
        ctx: The context.

    """
    path = _path(step["path"], ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _write(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `write` step, atomically, leaving an existing file unless `overwrite`.

    Args:
        step: The step.
        ctx: The context.

    """
    path = _path(step["path"], ctx)
    if path.exists() and step.get("overwrite") is not True:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        f.write(_expand(step["content"], ctx))
    os.replace(tmp, path)


def _move(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `move` step; with `overwrite`, what's in the way is removed first.

    Args:
        step: The step.
        ctx: The context.

    Raises:
        CellarError: If the destination exists and `overwrite` is false.
    """
    source = _source(step, ctx)
    target = _path(step["target"], ctx)
    target.parent.mkdir(parents=True, exist_ok=True)
    dest = _dest(source, target)
    if "overwrite" in step:
        overwrite = step["overwrite"] is True or step.get("force") is True
        if dest.exists() and not overwrite:
            raise CellarError(f"`move` target exists: {dest}", path=dest)

        if overwrite and dest != source and (source.exists() or source.is_symlink()):
            remove(dest)

    shutil.move(source, target)


def _move_contents(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `move_contents` step.

    Args:
        step: The step.
        ctx: The context.

    """
    source = _path(step["source"], ctx)
    target = _path(step["target"], ctx)
    target.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir()):
        if child != target:
            shutil.move(child, target)


def _copy(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `copy` step; a recursive copy from `dir/.` copies the dir's contents.

    Args:
        step: The step.
        ctx: The context.

    Raises:
        CellarError: If the destination exists and `overwrite` is false.
    """
    source = _source(step, ctx)
    target = _path(step["target"], ctx)
    target.parent.mkdir(parents=True, exist_ok=True)
    dest = _dest(source, target)
    overwrite = step.get("overwrite") is not False
    if dest.exists() and not overwrite:
        raise CellarError(f"`copy` target exists: {dest}", path=dest)

    if step.get("recursive") is not True:
        if overwrite and dest.is_symlink():
            dest.unlink()
        shutil.copy(source, dest)
        return

    # `cp_r` takes `dir/.` as the dir's contents; abspath dropped the `/.`
    if step["source"]["path"].endswith("/.") and target.is_dir():
        dest = target

    if not source.is_dir():
        _replace_copy(source, dest)
        return

    shutil.copytree(
        source, dest, symlinks=True, dirs_exist_ok=True, copy_function=_replace_copy
    )


def _replace_copy(source: str | Path, dest: str | Path) -> None:
    """Copy a file over whatever is at `dest`, rather than through a link there.

    Args:
        source: The file.
        dest: Where it goes.
    """
    with contextlib.suppress(FileNotFoundError):
        os.unlink(dest)
    shutil.copy2(source, dest)


def _symlink(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `symlink` step; a glob matching several sources links each into target.

    Args:
        step: The step.
        ctx: The context.

    """
    target = _path(step["target"], ctx)
    spec = step["source"]
    if step.get("source_glob") is not True:
        source = _expand(spec["path"], ctx) if spec.get("base") == "relative" else None
        _link(source or str(_path(spec, ctx)), target, step)
        return

    sources = _matches(spec, ctx)
    if len(sources) > 1 or target.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for s in sources:
            _link(str(s), target / s.name, step)

    elif sources:
        _link(str(sources[0]), target, step)


def _link(source: str, target: Path, step: Mapping[str, Any]) -> None:
    """Make one symlink, through sudo when the step asks.

    Args:
        source: What the link points at.
        target: The link.
        step: The step, for `sudo` and `force`.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    force = step.get("force") is True
    if _sudo(step, target.parent):
        run_sudo("/bin/ln", "-s", *(["-f"] if force else []), "--", source, str(target))
        return

    if force:
        with contextlib.suppress(OSError):  # As `rm_f`: a dir in the way stays
            target.unlink()
    target.symlink_to(source)


def _sudo(step: Mapping[str, Any], parent: Path) -> bool:
    """Whether a `sudo` step needs sudo for a path in `parent`.

    Args:
        step: The step.
        parent: The dir the step writes in.

    Returns:
        True for `sudo: true`, and for `if_needed` when `parent` isn't writable.
    """
    sudo = step.get("sudo")

    return sudo is True or (sudo == "if_needed" and not os.access(parent, os.W_OK))


def _remove(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `remove` step; a `sudo` one removes what the user can't through sudo.

    Args:
        step: The step.
        ctx: The context.

    """
    paths = [p for s in step["paths"] for p in _matches(s, ctx)]
    if needle := step.get("symlink_target_contains"):
        paths = [p for p in paths if p.is_symlink() and needle in os.readlink(p)]

    if needle := step.get("content_contains"):
        paths = [p for p in paths if _contains(p, needle.encode())]

    denied: list[Path] = []
    for path in paths:
        if undeletable(path):
            log.warning(f"Skipping remove for undeletable path {path}")
            continue

        try:
            if step.get("recursive") is True:
                remove(path)

            elif not path.is_dir() or path.is_symlink():  # As `rm_f`: a dir stays
                path.unlink(missing_ok=True)

        except PermissionError:
            if not step.get("sudo"):
                raise
            denied.append(path)

    _sudo_each(denied, "/bin/rm", *(["-r"] if step.get("recursive") else []), "-f")


def _contains(path: Path, needle: bytes) -> bool:
    """Whether a readable file's content contains `needle`.

    Args:
        path: The file.
        needle: The bytes to look for.

    Returns:
        True if it does.
    """
    try:
        return path.is_file() and needle in path.read_bytes()

    except OSError:
        return False


def _inreplace(step: Mapping[str, Any], ctx: Context) -> None:
    """Run an `inreplace` step; unless `skip_audit`, finding nothing is an error.

    Args:
        step: The step.
        ctx: The context.

    Raises:
        CellarError: If nothing was replaced and the step is audited.
    """
    path = _path(step["path"], ctx)
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    before = _expand(step["before"], ctx)
    after = _expand(step["after"], ctx)
    once = step.get("first_only") is True

    if step.get("regexp") is True:
        options = step.get("regexp_options") or 0
        flags = re.MULTILINE  # Ruby's `^` and `$` always match at lines
        for bit, flag in _REGEXP_FLAGS.items():
            if options & bit:
                flags |= flag

        new = re.sub(before, lambda _: after, text, count=int(once), flags=flags)

    else:
        new = text.replace(before, after, 1 if once else -1)

    if new == text and step.get("skip_audit") is not True:
        raise CellarError(f"`inreplace` found no {before!r} in {path}", path=path)

    path.write_text(new, encoding="utf-8", errors="surrogateescape")


def _set_permissions(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `set_permissions` step through `chmod`, so symbolic modes work.

    Args:
        step: The step.
        ctx: The context.

    """
    if paths := _existing(step, ctx):
        _check(
            [
                "/bin/chmod",
                *([] if step.get("non_recursive") else ["-R"]),
                "--",
                step["permissions"],
                *map(str, paths),
            ]
        )


def _set_ownership(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `set_ownership` step: without sudo first, then with it.

    Args:
        step: The step.
        ctx: The context.

    Raises:
        CellarError: If even sudo can't, naming App Management when macOS refused.
    """
    if not (paths := _existing(step, ctx)):
        return

    owner = f"{step.get('user') or ctx.values['user']}:{step.get('group') or 'staff'}"
    flags = [] if step.get("non_recursive") else ["-R"]
    cmd = ["/usr/sbin/chown", *flags, "--", owner, *map(str, paths)]
    if not subprocess.run(cmd, capture_output=True, check=False).returncode:
        return

    proc = run_sudo(*cmd, check=False)
    if proc.returncode:
        err = proc.stderr.decode(errors="replace").strip()
        hint = f"\n{_APP_MANAGEMENT}" if "Operation not permitted" in err else ""
        raise CellarError(f"can't change the ownership of {paths[0]}: {err}{hint}")


def _terminate_process(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `terminate_process` step, retrying a second apart.

    Args:
        step: The step.
        ctx: The context.

    Raises:
        CellarError: If it never succeeds and the step must.
    """
    for notice in step.get("notices") or []:
        log.info(_expand(notice, ctx))

    name = _expand(step["name"], ctx)
    if step.get("match") == "full":
        cmd = ["/usr/bin/pkill", "-f", name]
    else:
        cmd = ["/usr/bin/killall", name]

    attempts = step.get("attempts") or 1
    for attempt in range(attempts):
        if step.get("sudo") is True:
            proc = run_sudo(*cmd, check=False)
        else:
            proc = subprocess.run(cmd, capture_output=True, check=False)

        if not proc.returncode:
            return

        if attempt + 1 < attempts:
            time.sleep(1)

    if message := step.get("failure_message"):
        log.warning(_expand(message, ctx))

    if step.get("must_succeed") is True:
        raise CellarError(f"couldn't terminate {name}")


def _run(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `run` step through `run_script`'s environment and error handling.

    Args:
        step: The step.
        ctx: The context.

    """
    spec = step["command"]
    command = (
        _expand(spec["path"], ctx)
        if spec.get("base", "relative") == "relative"
        else str(_path(spec, ctx))
    )
    run_script(
        {
            "executable": command,
            "args": [_expand(a, ctx) for a in step.get("args") or []],
            "sudo": step.get("sudo") is True,
        },
        Path("/"),  # Not a dir to look in: an absolute path, or a name on PATH
        ctx.prefix,
        extra_env={k: _expand(str(v), ctx) for k, v in (step.get("env") or {}).items()},
    )


def _delete_keychain_certificate(step: Mapping[str, Any], ctx: Context) -> None:
    """Run a `delete_keychain_certificate` step, for one certificate or all so named.

    Args:
        step: The step.
        ctx: The context.

    """
    want = None
    if spec := step.get("matching_certificate"):
        cert = _path(spec, ctx)
        if not cert.exists():
            return

        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        want = hashlib.sha256(der).hexdigest().upper()

    find = ("/usr/bin/security", "find-certificate", "-a", "-c", step["name"], "-Z")
    found = run_sudo(*find, check=False)  # No match exits non-zero
    hashes = re.findall(r"^SHA-256 hash:\s*(\S+)", found.stdout.decode(), re.MULTILINE)
    for h in map(str.upper, hashes):
        if want in (None, h):
            run_sudo("/usr/bin/security", "delete-certificate", "-Z", h)


def _check(cmd: list[str]) -> None:
    """Run a command, raising with its stderr if it fails.

    Args:
        cmd: The command.

    Raises:
        CellarError: If it exits non-zero.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode:
        raise CellarError(f"{cmd[0]} failed: {proc.stderr.strip()}")


_RUNNERS: Mapping[str, Callable[[Mapping[str, Any], Context], None]] = {
    "mkdir_p": _mkdir_p,
    "touch": _touch,
    "write": _write,
    "move": _move,
    "move_contents": _move_contents,
    "copy": _copy,
    "symlink": _symlink,
    "remove": _remove,
    "inreplace": _inreplace,
    "set_permissions": _set_permissions,
    "set_ownership": _set_ownership,
    "terminate_process": _terminate_process,
    "run": _run,
    "delete_keychain_certificate": _delete_keychain_certificate,
}
