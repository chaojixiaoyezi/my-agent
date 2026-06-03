
from __future__ import annotations

import json
from typing import Any

DEFAULT_HINT_MAX_CHARS = 4000


def artifact_read_hints_from_work_state(work_state: dict[str, Any]) -> list[dict[str, Any]]:
    refs = work_state.get("artifact_refs", []) if isinstance(work_state.get("artifact_refs"), list) else []
    return [_hint(ref) for ref in refs if _is_tool_output_ref(ref)]


def artifact_read_hint_lines(hints: list[dict[str, Any]]) -> list[str]:
    return [
        json.dumps(
            {
                "tool": item["tool"],
                "artifact_ref": item["artifact_ref"],
                "offset": item["offset"],
                "max_chars": item["max_chars"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        for item in hints
    ]


def _hint(ref: dict[str, Any]) -> dict[str, Any]:
    fallback = str(ref.get("path", "") or "")
    artifact_ref = str(ref.get("scoped_call_id") or ref.get("call_id") or fallback)
    return {
        "tool": "read_artifact",
        "artifact_ref": artifact_ref,
        "fallback_path": fallback,
        "offset": 0,
        "max_chars": DEFAULT_HINT_MAX_CHARS,
        "mode": "slice",
        "source_kind": str(ref.get("kind", "") or "artifact"),
        "source_tool": str(ref.get("tool", "") or ""),
        "sha256": str(ref.get("sha256", "") or ""),
        "size_bytes": int(ref.get("size_bytes", 0) or 0),
        "reserved": {},
    }


def _is_tool_output_ref(value: object) -> bool:
    return isinstance(value, dict) and str(value.get("kind", "") or "") == "tool_output" and bool(value.get("path"))


__all__ = ["artifact_read_hint_lines", "artifact_read_hints_from_work_state"]
