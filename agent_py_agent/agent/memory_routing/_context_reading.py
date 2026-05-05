"""Authority-file reading and receipt helpers for routed memory context."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ._context_paths import _ReadTarget


def _read_authority_file(
    target: _ReadTarget,
    *,
    max_chars_per_file: int,
) -> tuple[str, dict[str, Any]]:
    """Read one safe authority file and return a prompt section plus receipt."""
    started = time.perf_counter()
    receipt = _new_receipt(target, status="planned")
    try:
        if not target.absolute_path.exists():
            receipt["status"] = "missing"
            receipt["error"] = f"authority file does not exist: {target.path}"
            return "", _finish_receipt(receipt, started)
        if not target.absolute_path.is_file():
            receipt["status"] = "not_file"
            receipt["error"] = f"authority path is not a file: {target.path}"
            return "", _finish_receipt(receipt, started)
        content = target.absolute_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        receipt["status"] = "error"
        receipt["error"] = str(exc)
        return "", _finish_receipt(receipt, started)

    receipt["status"] = "read"
    receipt["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    section = _build_injected_section(target.path, content, max_chars_per_file=max_chars_per_file)
    return section, _finish_receipt(receipt, started)


def _build_injected_section(path: str, content: str, *, max_chars_per_file: int) -> str:
    """Format bounded authority text for prompt injection."""
    limit = max(max_chars_per_file, 0)
    truncated = len(content) > limit
    body = content[:limit]
    suffix = ""
    if truncated:
        suffix = f"\n\n[truncated: {len(content) - limit} chars omitted]"
    return f"### Routed memory authority: {path}\n\n{body}{suffix}".strip()


def _new_receipt(target: _ReadTarget, *, status: str) -> dict[str, Any]:
    """Create the stable receipt shape required by runtime callers."""
    return {
        "route_id": target.route_id,
        "path": target.path,
        "status": status,
        "content_hash": "",
        "elapsed_ms": 0.0,
        "error": "",
        "reasons": list(target.reasons),
    }


def _finish_receipt(receipt: dict[str, Any], started: float) -> dict[str, Any]:
    """Record elapsed time for a read attempt."""
    receipt["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return receipt
