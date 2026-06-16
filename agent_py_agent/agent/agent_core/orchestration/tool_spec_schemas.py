"""Native tool_use input-schema fragments for orchestration tools.

这些是 native tool_use 的精确 JSON Schema 片段（消除弱推导导致的 TOOL_INVALID_ARGUMENTS），
不是渲染进 prompt 的模型可读元数据，所以与 ``tool_spec_data.py`` 分开放置：后者受 compact
行数守卫约束，schema 片段不应挤占 prompt 元数据的行预算。只声明类型明确、execute 路径会按该
类型解析的参数（list/bool/object/int 等）；开放世界标量沿用上游 string 兜底。
"""

from __future__ import annotations

from typing import Any

_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "goal": {"type": "string"},
    "items": {"type": "array", "items": {"type": "object"}},
    "count": {"type": "integer", "minimum": 1},
    "role": {"type": "string"},
    "agent_name": {"type": "string"},
    "tool_preset": {"type": "string", "enum": ["coding", "read_only", "none"]},
    "allowed_tools": {"type": "array", "items": {"type": "string"}},
    "acceptance_checks": {"type": "array", "items": {"type": "string"}},
    "plan": {"type": "array", "items": {"type": "string"}},
    "input_refs": {"type": "array", "items": {"type": "string"}},
    "output_files": {"type": "array", "items": {"type": "string"}},
    "artifact_refs": {"type": "array", "items": {"type": "string"}},
    "replacement_for_run_ids": {"type": "array", "items": {"type": "string"}},
    "defer_start": {"type": "boolean"},
}
_INSPECT_TREE_PARAMETER_SCHEMA: dict[str, Any] = {
    "root_id": {"type": "string"},
    "run_id": {"type": "string"},
    "scope": {"type": "string", "enum": ["root_tree", "own_subtree", "subtree", "all"]},
}
_OBSERVATION_PARAMETER_SCHEMA: dict[str, Any] = {
    "thread_id": {"type": "string"},
    "task_id": {"type": "string"},
    "event_type": {"type": "string"},
    "summary": {"type": "string"},
    "urgency": {"type": "string"},
    "severity": {"type": "string"},
    "source_agent_id": {"type": "string"},
    "parent_agent_id": {"type": "string"},
    "root_task_id": {"type": "string"},
    "evidence_refs": {"type": "array", "items": {"type": "string"}},
    "requires_main_agent": {"type": "boolean"},
    "requires_llm_report": {"type": "boolean"},
    "dedupe_key": {"type": "string"},
}
_DISPATCH_PARAMETER_SCHEMA: dict[str, Any] = {
    "dry_run": {"type": "boolean"},
    "max_runners": {"type": "integer", "minimum": 0},
    "run_ids": {"type": "array", "items": {"type": "string"}},
    "recovery_mode": {"type": "string"},
}
_SCHEDULE_CHILD_PARAMETER_SCHEMA: dict[str, Any] = {
    "children": {"type": "array", "items": {"type": "object"}},
    "dry_run": {"type": "boolean"},
    "max_depth": {"type": "integer", "minimum": 0},
    "max_children": {"type": "integer", "minimum": 0},
}
_RESOLVE_CAPABILITY_PARAMETER_SCHEMA: dict[str, Any] = {
    "run_id": {"type": "string"},
    "decision": {"type": "string", "enum": ["grant", "deny", "accept_output_gaps"]},
    "reason": {"type": "string"},
    "request_id": {"type": "string"},
    "write_roots": {"type": "array", "items": {"type": "string"}},
    "tools": {"type": "array", "items": {"type": "string"}},
    "exempt_refs": {"type": "array", "items": {"type": "string"}},
}
