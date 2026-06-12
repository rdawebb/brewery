"""Unit tests for the bottle extractor."""

from __future__ import annotations

import pytest

from brewery.providers.extractor import ExtractionError, detect_format

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "magic,expected",
    [
        (b"\x1f\x8b", "gzip"),
        (b"\x28\xb5\x2f\xfd", "zstd"),
    ],
    ids=["gzip", "zstd"],
)
def test_detect_format(tmp_path, magic, expected) -> None:
    """Test that magic bytes map to the correct format name."""
    arc = tmp_path / "archive"
    arc.write_bytes(magic + b"\x00" * 4)
    assert detect_format(arc) == expected


def test_detect_format_rejects_unknown(tmp_path) -> None:
    """Test format detection rejects unknown formats."""
    arc = tmp_path / "weird"
    arc.write_bytes(b"PK\x03\x04rest-of-a-zip")
    with pytest.raises(ExtractionError, match="unrecognized"):
        detect_format(arc)
