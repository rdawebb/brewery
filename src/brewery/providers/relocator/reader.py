"""Random access over a binary file's header, from one open descriptor."""

from __future__ import annotations

import os
import struct

# Prefetched at the head of every file the walker opens: enough for a Mach-O
# header plus its load commands, or an ELF header plus its program headers
_HEAD_WINDOW = 64 * 1024  # 64 KiB

# Longest linkage string read from an unbounded region (PATH_MAX is 1024)
_CSTR_MAX = 4096  # 4 KiB


def _cstr(buf: bytes, start: int, end: int) -> bytes:
    """Read a null-terminated string out of an in-memory buffer.

    Args:
        buf: The buffer to read from.
        start: The start index (inclusive).
        end: The end index (exclusive), bounding an unterminated string.

    Returns:
        The null-terminated string as bytes.
    """
    region = buf[start:end]
    nul = region.find(b"\x00")

    return region[:nul] if nul != -1 else region


class _Reader:
    """Random-access reads over an open file, without mapping it.

    The head of the file is prefetched once, which covers a Mach-O header plus
    its load commands and an ELF header plus its program headers; anything
    beyond that window (a fat slice, an ELF string table) is `pread` on demand.
    """

    __slots__ = ("_fd", "_head", "size")

    def __init__(self, fd: int, size: int) -> None:
        """Prefetch the head of an open file.

        Args:
            fd: The file descriptor to read from; owned by the caller.
            size: The file's size in bytes.
        """
        self._fd = fd
        self.size = size
        self._head = os.pread(fd, min(size, _HEAD_WINDOW), 0)

    def read(self, off: int, n: int) -> bytes:
        """Read `n` bytes from `off`, serving the head window from memory.

        Args:
            off: The file offset to read from.
            n: The number of bytes wanted.

        Returns:
            The bytes read, short at EOF.
        """
        end = off + n
        if end <= len(self._head):
            return self._head[off:end]

        return os.pread(self._fd, n, off)

    def unpack_from(self, fmt: str, off: int) -> tuple:
        """Unpack one struct format at a file offset.

        Args:
            fmt: The struct format string.
            off: The file offset of the first byte.

        Returns:
            The unpacked fields.

        Raises:
            struct.error: If the file ends inside the field.
        """
        return struct.unpack(fmt, self.read(off, struct.calcsize(fmt)))

    def byte(self, off: int) -> int:
        """Read one byte as an integer.

        Args:
            off: The file offset of the byte.

        Returns:
            The byte's value.

        Raises:
            struct.error: If the offset is past the end of the file.
        """
        return self.unpack_from("B", off)[0]

    def cstr(self, start: int, end: int) -> bytes:
        """Read a null-terminated string, bounded by `end` and by `_CSTR_MAX`.

        Args:
            start: The file offset of the string's first byte.
            end: The exclusive end of the region the string may occupy.

        Returns:
            The string as bytes, without its terminator.
        """
        stop = min(end, self.size, start + _CSTR_MAX)
        if stop <= start:
            return b""

        return _cstr(self.read(start, stop - start), 0, stop - start)
