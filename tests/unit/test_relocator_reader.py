"""Unit tests for the pread-backed binary header reader."""

from __future__ import annotations

import contextlib
import struct
from pathlib import Path

import pytest

from brewery.providers.relocator import reader as reader_mod

pytestmark = pytest.mark.unit


class TestReader:
    """Tests for the pread-backed file reader that stands in for mmap."""

    @contextlib.contextmanager
    def _reader(self, tmp_path: Path, data: bytes, name: str = "f"):
        """Open a file of `data` and hold a reader over it for the block.

        The descriptor has to outlive the reader, since the reader only borrows it.

        Args:
            tmp_path: The pytest temp dir.
            data: The file's bytes.
            name: The filename.

        Yields:
            A reader positioned over the whole file.
        """
        p = tmp_path / name
        p.write_bytes(data)
        with p.open("rb") as fh:
            yield reader_mod._Reader(fh.fileno(), len(data))

    def test_reads_inside_and_beyond_the_prefetched_head(
        self, tmp_path, monkeypatch
    ) -> None:
        """Test that a read past the head window falls through to pread with the same bytes."""
        monkeypatch.setattr(reader_mod, "_HEAD_WINDOW", 8)
        with self._reader(tmp_path, bytes(range(64))) as reader:
            assert reader.read(0, 8) == bytes(range(8))  # Wholly within the window
            assert reader.read(4, 8) == bytes(range(4, 12))  # Straddles its end
            assert reader.read(40, 8) == bytes(range(40, 48))  # Wholly beyond it
            assert reader.size == 64

    def test_short_read_at_eof_raises_struct_error(self, tmp_path) -> None:
        """Test that a truncated field raises what the mapping's unpack_from used to raise."""
        with self._reader(tmp_path, b"\x01\x02\x03") as reader:
            assert reader.read(0, 8) == b"\x01\x02\x03"
            with pytest.raises(struct.error):
                reader.unpack_from(">I", 1)

    def test_cstr_stops_at_the_terminator_and_at_the_bound(self, tmp_path) -> None:
        """Test that a terminated string ends at its NUL; an unterminated one at `end`."""
        with self._reader(tmp_path, b"/usr/lib\x00tail\x00" + b"A" * 32) as reader:
            assert reader.cstr(0, 14) == b"/usr/lib"
            assert reader.cstr(14, 20) == b"AAAAAA"  # No NUL before the bound

    def test_cstr_is_capped_for_an_unbounded_region(self, tmp_path) -> None:
        """Test that an unterminated string read to EOF stops at _CSTR_MAX, not at the file size."""
        with self._reader(tmp_path, b"B" * (reader_mod._CSTR_MAX + 100)) as reader:
            assert len(reader.cstr(0, reader.size)) == reader_mod._CSTR_MAX

    def test_cstr_beyond_the_end_is_empty(self, tmp_path) -> None:
        """Test that a string offset past EOF yields nothing rather than raising."""
        with self._reader(tmp_path, b"short") as reader:
            assert reader.cstr(99, 200) == b""
