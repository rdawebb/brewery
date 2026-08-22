"""Unit tests for the per-formula link/unlink service."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from brewery.core.locks import lock_path
from brewery.core.models import Package, PackageKind, PackageStatus
from brewery.services.link import run_link, run_unlink


def _hold_rack(prefix: Path, name: str) -> int:
    """Lock a formula's rack from an unrelated fd, as a peer process would.

    Args:
        prefix: The Homebrew prefix.
        name: The formula whose rack to lock.

    Returns:
        The locked descriptor; close it to release.
    """
    path = lock_path(prefix, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)

    return fd


@pytest.fixture
def prefix(mock_env) -> Path:
    """The prefix of the shared hermetic environment."""
    return mock_env.prefix


@pytest.fixture
def make_pkg(make_keg, mock_env) -> Callable[..., Package]:
    """Return a factory building an installed-formula Package backed by a keg.

    The keg is created under the shared env's Cellar with one linkable
    executable named after the formula.

    Returns:
        A callable `make_pkg(name, version="1.0", *, status=...)`.
    """

    def _make(
        name: str,
        version: str = "1.0",
        *,
        status: PackageStatus = PackageStatus.NONE,
    ) -> Package:
        keg = make_keg(mock_env.cellar, name, version, executables=[name])

        return Package(
            name=name,
            kind=PackageKind.FORMULA,
            versions=[version],
            status=status,
            path=str(keg),
        )

    return _make


class TestRunLink:
    """Tests for linking formulae."""

    def test_links_a_plain_formula(self, prefix, mock_env, make_pkg) -> None:
        """Test a normal formula links and reports its symlink count."""
        pkg = make_pkg("wget")

        linked, advisories, failures = run_link([pkg], env=mock_env)

        assert [name for name, _ in linked] == ["wget"]
        assert "bin/wget" in linked[0][1].linked
        assert not advisories and not failures
        assert (prefix / "bin" / "wget").is_symlink()

    def test_already_linked_is_an_advisory(self, prefix, mock_env, make_pkg) -> None:
        """Test a second link warns and skips rather than relinking."""
        pkg = make_pkg("wget")
        run_link([pkg], env=mock_env)

        linked, advisories, failures = run_link([pkg], env=mock_env)

        assert not linked and not failures
        assert advisories[0][0] == "wget"
        assert "already linked" in advisories[0][1]

    def test_keg_only_needs_force(self, prefix, mock_env, make_pkg) -> None:
        """Test keg-only formulae are skipped with an advisory naming --force."""
        pkg = make_pkg("icu4c", status=PackageStatus.KEG_ONLY)

        linked, advisories, failures = run_link([pkg], env=mock_env)

        assert not linked and not failures
        assert advisories == [("icu4c", "keg-only - link it with --force")]
        assert not (prefix / "bin" / "icu4c").exists()

    def test_keg_only_links_with_force(self, prefix, mock_env, make_pkg) -> None:
        """Test --force links a keg-only formula and writes its linked record."""
        pkg = make_pkg("icu4c", status=PackageStatus.KEG_ONLY)

        linked, advisories, failures = run_link([pkg], env=mock_env, force=True)

        assert [name for name, _ in linked] == ["icu4c"]
        assert not advisories and not failures
        assert (prefix / "bin" / "icu4c").is_symlink()
        assert (prefix / "var" / "homebrew" / "linked" / "icu4c").is_symlink()

    def test_keg_only_dry_run_previews_what_force_would_do(
        self, prefix, mock_env, make_pkg
    ) -> None:
        """Test a dry run still previews the links, as brew does, and says --force is needed."""
        pkg = make_pkg("icu4c", status=PackageStatus.KEG_ONLY)

        linked, advisories, failures = run_link([pkg], env=mock_env, dry_run=True)

        assert "bin/icu4c" in linked[0][1].linked
        assert advisories == [("icu4c", "keg-only - link it with --force")]
        assert not failures
        assert not (prefix / "bin" / "icu4c").exists()

    def test_conflict_becomes_a_failure_and_the_next_formula_still_links(
        self, prefix, mock_env, make_pkg
    ) -> None:
        """Test one formula's conflict must not abort the rest of the batch."""
        blocked = make_pkg("wget")
        ok = make_pkg("curl")
        (prefix / "bin").mkdir()
        (prefix / "bin" / "wget").write_text("a real file in the way")

        linked, _, failures = run_link([blocked, ok], env=mock_env)

        assert [name for name, _ in linked] == ["curl"]
        assert failures[0][0] == "wget"
        assert "brewery link --overwrite wget" in failures[0][1]
        assert (prefix / "bin" / "wget").read_text() == "a real file in the way"

    def test_overwrite_replaces_the_conflicting_file(
        self, prefix, mock_env, make_pkg
    ) -> None:
        """Test --overwrite links over a real file instead of failing."""
        pkg = make_pkg("wget")
        (prefix / "bin").mkdir()
        (prefix / "bin" / "wget").write_text("a real file in the way")

        linked, _, failures = run_link([pkg], env=mock_env, overwrite=True)

        assert [name for name, _ in linked] == ["wget"]
        assert not failures
        assert (prefix / "bin" / "wget").is_symlink()


class TestRunUnlink:
    """Tests for unlinking formulae."""

    def test_unlinks_a_linked_formula(self, prefix, mock_env, make_pkg) -> None:
        """Test unlinking removes the symlinks and reports the count."""
        pkg = make_pkg("wget")
        run_link([pkg], env=mock_env)

        unlinked, advisories, failures = run_unlink([pkg], env=mock_env)

        assert "bin/wget" in unlinked[0][1].removed
        assert not advisories and not failures
        assert not (prefix / "bin" / "wget").exists()

    def test_keg_only_unlink_is_a_no_op_not_a_failure(
        self, prefix, mock_env, make_pkg
    ) -> None:
        """Test keg-only formulae own no symlinks, so unlinking removes nothing and succeeds.

        Matches brew, whose `unlink` never consults keg_only.
        """
        pkg = make_pkg("icu4c", status=PackageStatus.KEG_ONLY)

        unlinked, advisories, failures = run_unlink([pkg], env=mock_env)

        assert unlinked[0][1].removed == []
        assert not advisories and not failures

    def test_dry_run_reports_without_removing(self, prefix, mock_env, make_pkg) -> None:
        """Test a dry run names the symlinks and leaves them in place."""
        pkg = make_pkg("wget")
        run_link([pkg], env=mock_env)

        unlinked, _, failures = run_unlink([pkg], env=mock_env, dry_run=True)

        assert "bin/wget" in unlinked[0][1].removed
        assert not failures
        assert (prefix / "bin" / "wget").is_symlink()


class TestRackLock:
    """Tests for per-rack locking."""

    def test_link_refuses_a_locked_rack(self, prefix, mock_env, make_pkg) -> None:
        """Test that the prefix is left untouched and the reason is reported."""
        pkg = make_pkg("wget")
        fd = _hold_rack(prefix, "wget")
        try:
            linked, _, failures = run_link([pkg], env=mock_env)

        finally:
            os.close(fd)

        assert not linked
        assert [name for name, _ in failures] == ["wget"]
        assert "already locked" in failures[0][1]
        assert not (prefix / "bin" / "wget").exists()

    def test_unlink_refuses_a_locked_rack(self, prefix, mock_env, make_pkg) -> None:
        """Test that an in-progress operation on the rack keeps the symlinks in place."""
        pkg = make_pkg("wget")
        run_link([pkg], env=mock_env)

        fd = _hold_rack(prefix, "wget")
        try:
            unlinked, _, failures = run_unlink([pkg], env=mock_env)

        finally:
            os.close(fd)

        assert not unlinked
        assert [name for name, _ in failures] == ["wget"]
        assert (prefix / "bin" / "wget").is_symlink()
