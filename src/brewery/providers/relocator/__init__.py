"""Relocator package: token map, Mach-O and ELF rewriting, and the keg sink.

`substitutions` builds the placeholder map; `reader` reads binary headers;
`tools` runs install_name_tool, patchelf and codesign; `macho` and `elf` parse
and rewrite one binary each; `files` classifies and relocates a finished file;
`keg` drives the whole keg from the extractor's member loop.
"""

from __future__ import annotations

from .keg import RelocationResult, StreamRelocator
from .substitutions import formula_tokens

__all__ = ["RelocationResult", "StreamRelocator", "formula_tokens"]
