"""Tar-archive builders for the extraction and relocation tests."""

from __future__ import annotations

import io
import tarfile


def make_tar(entries: list[tuple]) -> bytes:
    """Create a tar archive from the given entries.

    entries: ('file', name, data, mode) | ('dir', name, mode) | ('link', name, target).

    Args:
        entries: The list of entries to include in the tar archive.

    Returns:
        A bytes object containing the tar archive.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for entry in entries:
            kind = entry[0]
            if kind == "file":
                _, name, data, mode = entry
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                ti.mode = mode
                t.addfile(ti, io.BytesIO(data))

            elif kind == "dir":
                _, name, mode = entry
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.DIRTYPE
                ti.mode = mode
                t.addfile(ti)

            elif kind == "link":
                _, name, target = entry
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = target
                t.addfile(ti)

            elif kind == "hardlink":
                _, name, target = entry
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.LNKTYPE
                ti.linkname = target
                t.addfile(ti)

            elif kind == "fifo":
                _, name = entry
                ti = tarfile.TarInfo(name)
                ti.type = tarfile.FIFOTYPE
                t.addfile(ti)

            else:
                raise ValueError(kind)

    return buf.getvalue()


def standard_keg(name: str = "openssl@3", version: str = "3.0") -> list[tuple]:
    """Create a standard keg structure for the given formula.

    Args:
        name: The name of the formula.
        version: The version of the formula.

    Returns:
        A list of tuples representing the keg structure.
    """
    base = f"{name}/{version}"
    return [
        ("file", f"{base}/bin/openssl", b"MACHO-binary", 0o555),
        ("file", f"{base}/lib/libssl.dylib", b"@@HOMEBREW_PREFIX@@/lib", 0o444),
        ("link", f"{base}/lib/libssl.3.dylib", "libssl.dylib"),  # Relative
        # Placeholder target, relative as far as the filter is concerned
        (
            "link",
            f"{base}/bin/openssl-link",
            "@@HOMEBREW_PREFIX@@/opt/openssl@3/bin/openssl",
        ),
        ("dir", f"{name}/.brew", 0o755),
        ("file", f"{name}/.brew/{name}.rb", b"class Openssl3\nend\n", 0o644),
    ]
