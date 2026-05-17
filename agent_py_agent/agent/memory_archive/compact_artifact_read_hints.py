# LLM: Compact artifact read hints turn artifact refs into safe read_artifact tool arguments.
# 模块用途: 把 compact/resume 中的外置 artifact 引用转换成可执行的分片读取提示，不读取正文。

from __future__ import annotations

import json
from typing import Any

DEFAULT_HINT_MAX_CHARS = 4000


# LLM: artifact_read_hints_from_work_state prefers short scoped refs but keeps full paths as fallback.
# 函数用途: 从 work_state.artifact_refs 生成 read_artifact 提示，避免恢复模型只看到一串长路径。
def artifact_read_hints_from_work_state(work_state: dict[str, Any]) -> list[dict[str, Any]]:
    refs = work_state.get("artifact_refs", []) if isinstance(work_state.get("artifact_refs"), list) else []
    return [_hint(ref) for ref in refs if _is_tool_output_ref(ref)]


# LLM: artifact_read_hint_lines renders copyable tool-call JSON without expanding artifact bodies.
# 函数用途: 在 context block 中展示 read_artifact 参数，帮助恢复后的模型直接分片读取。
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


# LLM: _hint keeps the machine contract stable across handoff and continue packet.
# 函数用途: 生成单条 read_artifact 提示；scoped_call_id 优先，path 作为 fallback_path 备用。
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


# LLM: _is_tool_output_ref filters only externalized tool outputs for read_artifact recovery hints.
# 函数用途: 判断 artifact_refs 里的条目是否是可由 read_artifact 读取的 tool_output。
def _is_tool_output_ref(value: object) -> bool:
    return isinstance(value, dict) and str(value.get("kind", "") or "") == "tool_output" and bool(value.get("path"))


__all__ = ["artifact_read_hint_lines", "artifact_read_hints_from_work_state"]
