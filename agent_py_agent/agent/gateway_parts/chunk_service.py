from __future__ import annotations

"""Chunk file handling for streaming gateway output.

This module provides utilities for writing chunk files that CLI polls for streaming display.
"""

import json
import time
from pathlib import Path


def open_chunk_stream(chunk_path: Path) -> tuple[Path, float]:
    """Open a chunk file for appending and return (path, start_time)."""
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    return chunk_path, time.time()


def write_chunk(chunk_path: Path, text: str) -> None:
    """Append a single chunk to the chunk file."""
    try:
        line = json.dumps({"t": time.time(), "text": text}, ensure_ascii=False)
        with open(chunk_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def close_chunk_stream(chunk_path: Path) -> None:
    """Clean up chunk file after streaming is done."""
    try:
        chunk_path.unlink(missing_ok=True)
    except OSError:
        pass