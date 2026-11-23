"""Unit tests for running a cask's declarative install steps."""

from __future__ import annotations

import hashlib
import ssl
import subprocess
import sys

import pytest

from brewery.core.errors import CellarError
from brewery.providers import cask_steps
from brewery.providers.cask_steps import context, run_steps, unsupported

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="native casks are macOS-only"
)


@pytest.fixture
def ctx(tmp_path):
    """A context rooted in tmp_path, for version 2.7.9 of `foo`.

    Args:
        tmp_path: The pytest tmp_path fixture.

    Returns:
        The context.
    """
    staged = tmp_path / "Caskroom" / "foo" / "2.7.9"
    staged.mkdir(parents=True)
    temp = tmp_path / "temp"
    temp.mkdir()
    dirs = {"appdir": tmp_path / "Applications"}

    return context(
        "foo", "2.7.9", staged, temp, tmp_path / "prefix", tmp_path / "Cellar", dirs
    )


def test_templates_and_bases_resolve(ctx) -> None:
    """Test version parts, a root base and an unknown token left verbatim."""
    content = "{{token}} {{version.major}} {{version.major_minor}} {{other}}"
    run_steps(
        [
            {
                "type": "write",
                "path": {"base": "caskroom_path", "path": "v{{version}}.txt"},
                "content": content,
            }
        ],
        ctx,
    )

    written = ctx.roots["caskroom_path"] / "v2.7.9.txt"
    assert written.read_text() == "foo 2 2.7 {{other}}"


def test_a_guard_is_checked_once_per_block(ctx) -> None:
    """Test that a step sharing a guard still runs after an earlier one changes it."""
    envs = ctx.roots["caskroom_path"] / "envs"
    envs.mkdir()
    guard = {"path": "{{caskroom_path}}/envs", "condition": "if_exists", "id": "1"}

    run_steps(
        [
            {
                "type": "remove",
                "paths": [{"path": "{{caskroom_path}}/envs"}],
                "recursive": True,
                "guards": [guard],
            },
            {"type": "touch", "path": {"path": "{{temp}}/moved"}, "guards": [guard]},
        ],
        ctx,
    )

    assert not envs.exists()
    assert (ctx.roots["temp"] / "moved").exists()


@pytest.mark.parametrize("exists", [False, True])
def test_recursive_copy_from_dir_dot_copies_its_contents(ctx, exists) -> None:
    """Test `cp_r`'s `dir/.`, into a new target and into an existing one."""
    sdk = ctx.roots["staged_path"] / "sdk" / "x"
    sdk.mkdir(parents=True)
    (sdk / "a").write_text("a")
    target = ctx.roots["temp"] / "share" / "sdk"
    if exists:
        target.mkdir(parents=True)

    run_steps(
        [
            {
                "type": "copy",
                "source": {"base": "staged_path", "path": "sdk/."},
                "target": {"base": "temp", "path": "share/sdk"},
                "recursive": True,
            }
        ],
        ctx,
    )

    assert (target / "x" / "a").read_text() == "a"


def test_inreplace_matches_lines_and_audits(ctx) -> None:
    """Test a Ruby-style `^` regexp, and that replacing nothing is an error."""
    script = ctx.roots["staged_path"] / "run.sh"
    script.write_text("x\ndir=old/bin\n")
    step = {
        "type": "inreplace",
        "path": {"base": "staged_path", "path": "run.sh"},
        "before": "^dir=.*?/bin",
        "after": "dir={{staged_path}}/bin",
        "regexp": True,
    }

    run_steps([step], ctx)
    assert script.read_text() == f"x\ndir={ctx.roots['staged_path']}/bin\n"

    with pytest.raises(CellarError, match="found no"):
        run_steps([step], ctx)

    run_steps([{**step, "skip_audit": True}], ctx)


def _done(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_mkdir_p_makes_parents_and_tolerates_existing(ctx) -> None:
    """Test a nested dir under a root base, made twice."""
    step = {"type": "mkdir_p", "path": {"base": "appdir", "path": "Foo/{{version}}"}}

    run_steps([step, step], ctx)

    assert (ctx.roots["appdir"] / "Foo" / "2.7.9").is_dir()


def test_move_into_a_dir_and_over_a_target(ctx) -> None:
    """Test moving into an existing dir, and `overwrite` refusing then replacing."""
    staged = ctx.roots["staged_path"]
    (staged / "a").write_text("a")
    (staged / "dir").mkdir()
    (staged / "b").write_text("new")
    (staged / "c").write_text("old")

    def move(source, target, **extra):
        return {
            "type": "move",
            "source": {"base": "staged_path", "path": source},
            "target": {"base": "staged_path", "path": target},
            **extra,
        }

    run_steps([move("a", "dir")], ctx)
    assert (staged / "dir" / "a").read_text() == "a"

    with pytest.raises(CellarError, match="target exists"):
        run_steps([move("b", "c", overwrite=False)], ctx)

    run_steps([move("b", "c", overwrite=True)], ctx)
    assert (staged / "c").read_text() == "new"
    assert not (staged / "b").exists()


def test_move_source_glob_must_match_one_path(ctx) -> None:
    """Test that a `source_glob` matching two paths is refused."""
    staged = ctx.roots["staged_path"]
    (staged / "x1").touch()
    (staged / "x2").touch()
    step = {
        "type": "move",
        "source": {"base": "staged_path", "path": "x*"},
        "target": {"base": "temp", "path": "x"},
        "source_glob": True,
    }

    with pytest.raises(CellarError, match="must match one path"):
        run_steps([step], ctx)


def test_move_contents_skips_a_target_inside_the_source(ctx) -> None:
    """Test that a target inside the source isn't moved into itself."""
    staged = ctx.roots["staged_path"]
    (staged / "x").touch()
    (staged / "y").mkdir()
    step = {
        "type": "move_contents",
        "source": {"base": "staged_path", "path": "."},
        "target": {"base": "staged_path", "path": "out"},
    }

    run_steps([step], ctx)

    assert sorted(p.name for p in staged.iterdir()) == ["out"]
    assert sorted(p.name for p in (staged / "out").iterdir()) == ["x", "y"]


def test_symlink_relative_source_is_kept_relative(ctx) -> None:
    """Test a `relative` source left unresolved, `force` replacing a file there."""
    link = ctx.roots["temp"] / "link"
    link.touch()
    step = {
        "type": "symlink",
        "source": {"base": "relative", "path": "../lib/{{token}}"},
        "target": {"base": "temp", "path": "link"},
        "force": True,
    }

    run_steps([step], ctx)

    assert link.readlink().as_posix() == "../lib/foo"


def test_symlink_glob_links_each_match_into_target(ctx) -> None:
    """Test that a glob matching several sources links each inside the target dir."""
    bin_dir = ctx.roots["staged_path"] / "bin"
    bin_dir.mkdir()
    (bin_dir / "a").touch()
    (bin_dir / "b").touch()
    step = {
        "type": "symlink",
        "source": {"base": "staged_path", "path": "bin/*"},
        "target": {"base": "temp", "path": "bin"},
        "source_glob": True,
    }

    run_steps([step], ctx)

    for name in ("a", "b"):
        assert (ctx.roots["temp"] / "bin" / name).readlink() == bin_dir / name


def test_set_permissions_takes_symbolic_modes_and_skips_missing(ctx) -> None:
    """Test a symbolic mode through chmod, with a missing path ignored."""
    script = ctx.roots["staged_path"] / "run.sh"
    script.touch(mode=0o644)
    step = {
        "type": "set_permissions",
        "paths": [
            {"base": "staged_path", "path": "run.sh"},
            {"base": "staged_path", "path": "missing"},
        ],
        "permissions": "u+x",
        "non_recursive": True,
    }

    run_steps([step], ctx)

    assert script.stat().st_mode & 0o777 == 0o744


def test_set_ownership_falls_back_to_sudo_and_names_app_management(
    ctx, monkeypatch
) -> None:
    """Test the sudo retry with the context's user, and the App Management hint."""
    (ctx.roots["staged_path"] / "Foo.app").mkdir()
    calls = []
    monkeypatch.setattr(
        cask_steps.subprocess, "run", lambda cmd, **_: _done(returncode=1)
    )

    def sudo(*cmd, check=True):
        calls.append(cmd)
        return _done(returncode=1, stderr=b"chown: Foo.app: Operation not permitted")

    monkeypatch.setattr(cask_steps, "run_sudo", sudo)
    step = {
        "type": "set_ownership",
        "paths": [{"base": "staged_path", "path": "Foo.app"}],
        "group": "admin",
    }

    with pytest.raises(CellarError, match="App Management"):
        run_steps([step], ctx)

    assert calls[0][:4] == (
        "/usr/sbin/chown",
        "-R",
        "--",
        f"{ctx.values['user']}:admin",
    )


def test_terminate_process_retries_then_must_succeed(ctx, monkeypatch) -> None:
    """Test `pkill -f` for a full match, retried a second apart, then an error."""
    calls, sleeps = [], []
    monkeypatch.setattr(
        cask_steps.subprocess,
        "run",
        lambda cmd, **_: calls.append(cmd) or _done(returncode=1),
    )
    monkeypatch.setattr(cask_steps.time, "sleep", sleeps.append)
    step = {
        "type": "terminate_process",
        "name": "{{token}} helper",
        "match": "full",
        "attempts": 3,
        "must_succeed": True,
    }

    with pytest.raises(CellarError, match="couldn't terminate foo helper"):
        run_steps([step], ctx)

    assert calls == [["/usr/bin/pkill", "-f", "foo helper"]] * 3
    assert sleeps == [1, 1]


def test_run_expands_args_and_env(ctx, monkeypatch) -> None:
    """Test that a relative command stays a name, with args and env expanded."""
    seen = {}

    def run_script(spec, staged, prefix, *, extra_env=None):
        seen.update(spec=spec, staged=staged, env=extra_env)

    monkeypatch.setattr(cask_steps, "run_script", run_script)
    step = {
        "type": "run",
        "command": {"path": "{{token}}-setup"},
        "args": ["--to", "{{appdir}}"],
        "env": {"V": "{{version.major}}"},
        "sudo": True,
    }

    run_steps([step], ctx)

    assert seen["spec"] == {
        "executable": "foo-setup",
        "args": ["--to", ctx.values["appdir"]],
        "sudo": True,
    }
    assert seen["env"] == {"V": "2"}


@pytest.mark.parametrize("matching", [False, True])
def test_delete_keychain_certificate_filters_by_hash(
    ctx, monkeypatch, matching
) -> None:
    """Test deleting every certificate so named, or only the one matching a file."""
    der = b"not really a certificate"
    want = hashlib.sha256(der).hexdigest()
    (ctx.roots["staged_path"] / "ca.pem").write_text(ssl.DER_cert_to_PEM_cert(der))
    deleted = []

    def sudo(*cmd, check=True):
        if cmd[1] == "delete-certificate":
            deleted.append(cmd[-1])
        return _done(stdout=f"SHA-256 hash: aa11\nSHA-256 hash: {want}\n".encode())

    monkeypatch.setattr(cask_steps, "run_sudo", sudo)
    step = {"type": "delete_keychain_certificate", "name": "Foo CA"}
    if matching:
        step["matching_certificate"] = {"base": "staged_path", "path": "ca.pem"}

    run_steps([step], ctx)

    assert deleted == ([want.upper()] if matching else ["AA11", want.upper()])


@pytest.mark.parametrize(
    ("step", "reason"),
    [
        pytest.param(
            {"type": "mkdir_p", "path": {"base": "appdir", "path": "Foo"}}, "", id="ok"
        ),
        pytest.param({"type": "configure_php"}, "step `configure_php`", id="type"),
        pytest.param(
            {"type": "run", "command": {"path": "x"}, "allow_failure": True},
            "`run` step's `allow_failure`",
            id="key",
        ),
        pytest.param(
            {"type": "touch", "path": {"base": "formula_opt_prefix", "path": "x"}},
            "base `formula_opt_prefix`",
            id="base",
        ),
        pytest.param(
            {"type": "touch", "path": {"path": "{{arch}}/x"}},
            "template `{{arch}}`",
            id="token",
        ),
        pytest.param(
            {
                "type": "inreplace",
                "path": {"path": "/x"},
                "before": "(a)",
                "after": "\\1",
            },
            "`inreplace` backreference",
            id="backreference",
        ),
    ],
)
def test_unsupported_steps_go_to_brew(step, reason) -> None:
    """Test that an unknown type, key, base or token, or a backreference, is refused."""
    artifacts = [{"postflight_steps": [{"steps": [step]}]}]
    expected = f"`postflight_steps` {reason}" if reason else ""

    assert unsupported(artifacts) == expected


def test_malformed_stage_goes_to_brew() -> None:
    """Test that a stage that isn't a list of step blocks is refused."""
    assert unsupported([{"preflight_steps": {"steps": []}}]) == (
        "unrecognised `preflight_steps`"
    )
