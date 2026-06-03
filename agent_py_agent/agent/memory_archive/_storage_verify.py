
"""Readback verification helpers for memory archive storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class MemoryArchiveError(RuntimeError):
    """Raised when archive writes cannot be verified after append."""


def _verify_record_exists(path: Path, *, key: str, value: str, expected: dict[str, Any]) -> None:
    """Read a JSONL file backwards and confirm the just-written record is present."""
    normalized_expected = _normalized_json(expected)
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get(key) != value:
            continue
        if _normalized_json(payload) != normalized_expected:
            raise MemoryArchiveError(f"readback payload mismatch for {key}={value}")
        return
    raise MemoryArchiveError(f"readback failed for {key}={value} in {path}")


def _verify_json_file_payload(path: Path, *, expected: dict[str, Any]) -> None:
    """Ensure an authoritative JSON snapshot file can be read back exactly."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryArchiveError(f"readback failed for snapshot file {path}: {exc}") from exc
    if _normalized_json(payload) != _normalized_json(expected):
        raise MemoryArchiveError(f"readback payload mismatch for snapshot file {path}")


def _normalized_json(payload: dict[str, Any]) -> str:
    """Serialize JSON payloads into a canonical string for equality checks."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
