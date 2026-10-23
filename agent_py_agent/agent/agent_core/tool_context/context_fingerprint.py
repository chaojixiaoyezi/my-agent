# LLM: Shared local fingerprint for three decision points; preserve historical fields, defaults, tool ordering and decision_json bytes.
# 模块用途: 统一上下文版本计算，不保存、上传或展开任务权限材料。
from __future__ import annotations

import hashlib

from ...backends.decision_protocol import decision_json


# LLM: Only hash the original projection; propagate validation errors and do not deduplicate or normalize values.
# 函数用途: 计算请求、任务、权限和工具快照的精确SHA256版本。
def context_fingerprint(params: object) -> str:
    snapshot = getattr(params, "tool_runtime_snapshot", None)
    return hashlib.sha256(decision_json({
        "request_id": getattr(params, "request_id", ""), "run_id": getattr(params, "run_id", ""),
        "task_id": getattr(params, "task_id", ""), "user_prompt": getattr(params, "user_prompt", ""),
        "context_scope": getattr(params, "context_scope", ""),
        "task_attributes": getattr(params, "task_attributes", None),
        "allowed_tools": getattr(params, "allowed_tools", None),
        "write_boundary": getattr(params, "write_boundary", None),
        "snapshot_hash": getattr(snapshot, "snapshot_hash", ""),
        "available_tools": sorted(getattr(snapshot, "available_tool_names", ())),
    })).hexdigest()
