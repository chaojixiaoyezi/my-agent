# LLM: schema 与创建参数解析保持一致；model 只允许公开引用，不能通过工具传递端点或凭据；
#   effort 只是智能程度档位枚举，由宿主换算，不能携带任何供应商原始字段。
# 模块用途: 声明派工和权限工具的结构字段，模型与档位字段可选且不扩大 owner 边界。
"""Native tool_use input-schema fragments for orchestration tools.

这些是 native tool_use 的精确 JSON Schema 片段（消除弱推导导致的 TOOL_INVALID_ARGUMENTS），
不是渲染进 prompt 的模型可读元数据，所以与 ``tool_spec_data.py`` 分开放置：后者受 compact
行数守卫约束，schema 片段不应挤占 prompt 元数据的行预算。只声明类型明确、execute 路径会按该
类型解析的参数（list/bool/object/int 等）；开放世界标量沿用上游 string 兜底。
"""

from __future__ import annotations

from typing import Any

_CREATE_ITEM_PARAMETER_SCHEMA: dict[str, Any] = {
    "goal": {"type": "string"},
    "persistent_goal": {"type": "string", "minLength": 1, "maxLength": 4000},
    # 计划绑定紧跟 goal 展示给模型；它仍是可选字段，但承接已有 Todo 时应优先被看到并填写。
    "covers": {"type": "array", "items": {"type": "string"}},
    "description": {"type": "string", "maxLength": 240},
    "role": {"type": "string"},
    "agent_name": {"type": "string"},
    "model": {"type": "string", "minLength": 1},
    # 智能程度档位；枚举与 backends/reasoning_control.REASONING_LEVELS 一致，省略则继承当前会话的档位。
    "effort": {"type": "string", "enum": ["auto", "off", "low", "medium", "high", "max"]},
    "tool_preset": {"type": "string", "enum": ["coding", "read_only", "none"]},
    "allowed_tools": {"type": "array", "items": {"type": "string"}},
    "allowed_skills": {"type": "array", "items": {"type": "string"}},
    "plan": {"type": "array", "items": {"type": "string"}},
    "input_refs": {"type": "array", "items": {"type": "string"}},
    "output_files": {"type": "array", "items": {"type": "string"}},
    "artifact_refs": {"type": "array", "items": {"type": "string"}},
    "replacement_for_run_ids": {"type": "array", "items": {"type": "string"}},
    "related_finding_id": {"type": "string", "minLength": 1, "maxLength": 128},
    "long_running": {"type": "boolean"},
    "service_window_seconds": {"type": "integer", "minimum": 1},
    "audit_source_id": {"type": "string", "minLength": 1, "maxLength": 128},
}
_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    **_CREATE_ITEM_PARAMETER_SCHEMA,
    "items": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": _CREATE_ITEM_PARAMETER_SCHEMA,
            "required": ["goal"],
            "additionalProperties": False,
        },
    },
}
_RESOLVE_CAPABILITY_PARAMETER_SCHEMA: dict[str, Any] = {
    "run_id": {"type": "string"},
    "decision": {"type": "string", "enum": ["grant", "deny"]},
    "reason": {"type": "string"},
    "request_id": {"type": "string"},
    "write_roots": {"type": "array", "items": {"type": "string"}},
    "tools": {"type": "array", "items": {"type": "string"}},
}
