"""Install a staged, relocated keg into the Cellar."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import os
import shutil
import stat
import sys
from pathlib import Path

from brewery.core.errors import BrewError

_IS_DARWIN = sys.platform == "darwin"


class CellarError(BrewError):
    """Installing a keg into the Cellar failed; per-formula fallback signal."""


def _clonefile(src: Path, dst: Path) -> None:
    """Use the clonefile syscall to create a copy of the file at `src` in `dst`.

    Args:
        src: The source file path.
        dst: The destination file path.

    Raises:
        OSError: If the clonefile syscall fails.
    """
    libc = ctypes.CDLL(
        name=ctypes.util.find_library(name="c") or "libc.dylib", use_errno=True
    )
    libc.clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
    libc.clonefile.restype = ctypes.c_int

    if libc.clonefile(os.fsencode(filename=src), os.fsencode(filename=dst), 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(object=dst))


def clone_tree(src: Path, dst: Path, *, use_clonefile: bool | None = None) -> None:
    """Clone the directory tree at `src` to a non-existent `dst`.

    Uses `clonefile` on Darwin (falling back to a recursive copy if the
    filesystem doesn't support it), and a plain `copytree` elsewhere.

    Args:
        src: The source directory path.
        dst: The destination directory path.
        use_clonefile: Whether to use the clonefile syscall.

    Raises:
        FileExistsError: If the destination already exists.
        OSError: If the clonefile syscall fails.
    """
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"clone destination already exists: {dst}")

    use: bool = _IS_DARWIN if use_clonefile is None else use_clonefile
    if use:
        try:
            _clonefile(src, dst)
            return

        except OSError as exc:
            # Unsupported filesystem / cross-device -> fall back to a copy
            if exc.errno not in (errno.ENOTSUP, errno.EXDEV, errno.EINVAL):
                raise

    shutil.copytree(src, dst, symlinks=True)


def rmtree(path: Path) -> None:
    """Remove a keg tree, tolerating the read-only files bottles ship.

    Args:
        path (Path): The path to the keg tree to remove.
    """

    def onerror(func, p, exc) -> None:
        """Handle exceptions raised during the removal of read-only files.

        Args:
            func: The function to call to remove the file.
            p: The path to the file to remove.
            exc: The exception raised during the removal.
        """
        parent = os.path.dirname(p)
        with contextlib.suppress(OSError):
            os.chmod(path=parent, mode=0o755)

        with contextlib.suppress(OSError):
            os.chmod(path=p, mode=stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)

        func(p)

    shutil.rmtree(path, onerror=onerror)


def _link_opt(prefix: Path, name: str, version: str) -> Path:
    """Create/refresh <prefix>/opt/<name> -> ../Cellar/<name>/<version> (relative).

    Args:
        prefix (Path): The prefix path.
        name (str): The name of the formula.
        version (str): The version of the formula.

    Returns:
        Path: The path to the created/updated symlink.
    """
    opt = prefix / "opt" / name
    opt.parent.mkdir(parents=True, exist_ok=True)
    if opt.is_symlink() or opt.exists():
        opt.unlink()

    opt.symlink_to(Path("..") / "Cellar" / name / version)

    return opt


def install_to_cellar(
    staged_keg: Path,
    *,
    prefix: Path,
    name: str,
    version: str,
    use_clonefile: bool | None = None,
) -> Path:
    """Clone `staged_keg` into the Cellar and refresh the opt link.

    Returns the installed keg path `<prefix>/Cellar/<name>/<version>`. An
    existing keg at that exact path is removed first (reinstall), since the
    clone requires a fresh destination.

    Args:
        staged_keg (Path): The path to the staged keg.
        prefix (Path): The prefix path.
        name (str): The name of the formula.
        version (str): The version of the formula.
        use_clonefile (bool | None): Whether to use clonefile for installation.

    Returns:
        Path: The path to the installed keg.

    Raises:
        CellarError: If the installation fails.
    """
    cellar = prefix / "Cellar" / name
    dest = cellar / version
    cellar.mkdir(parents=True, exist_ok=True)

    if dest.is_symlink() or dest.exists():
        rmtree(dest)

    try:
        clone_tree(staged_keg, dest, use_clonefile=use_clonefile)

    except OSError as exc:
        # Leave no partial keg behind
        if dest.exists():
            with contextlib.suppress(OSError):
                rmtree(dest)

        raise CellarError(
            f"failed to install {name} {version} into Cellar: {exc}"
        ) from exc

    _link_opt(prefix, name, version)

    return dest
