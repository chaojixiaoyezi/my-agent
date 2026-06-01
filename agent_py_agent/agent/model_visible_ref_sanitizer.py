# LLM: Model-visible ref sanitizing keeps legacy runtime paths out of tool outputs.
# 模块用途: 净化返回给模型的状态/调度结果，隐藏旧式子代理 work-order 路径；内部迁移和恢复仍可读取原始字段。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_HIDDEN_LEGACY_SUBAGENT_REF = "[internal_legacy_subagent_path_hidden]"
_INTERNAL_MODEL_VISIBLE_TOOLS = frozenset(
    {
        "create_subagents",
        "dispatch_subagents",
        "inspect_agent_tree",
        "schedule_child_subagents",
        "subagent_board",
    }
)


# LLM: sanitize_model_visible_refs is a presentation helper, not a permission gate.
# 函数用途: 递归净化模型可见 payload；遇到旧 data/subagents 路径只隐藏展示值，不修改底层账本。
def sanitize_model_visible_refs(value: Any) -> Any:
    return _sanitize_model_visible_refs(value, field="")


# LLM: _sanitize_model_visible_refs carries the parent field name through recursive payloads.
# 函数用途: 按字段上下文递归处理字典、列表、路径和字符串，让输出产物名可保留、旧内部路径可隐藏。
def _sanitize_model_visible_refs(value: Any, *, field: str) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_model_visible_refs(item, field=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_model_visible_refs(item, field=field) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_model_visible_refs(item, field=field) for item in value]
    if isinstance(value, Path):
        return _sanitize_model_visible_refs(str(value), field=field)
    if isinstance(value, str):
        return _sanitized_string(value, field=field)
    return value


# LLM: _sanitized_string hides legacy runtime paths but keeps artifact-facing filenames useful.
# 函数用途: 对单个字符串执行旧路径净化；产物引用字段保留文件名，其他内部路径替换成占位说明。
def _sanitized_string(value: str, *, field: str) -> str:
    if not is_legacy_subagent_path(value):
        return value
    if field in {"output_files", "output_refs", "artifact_refs"}:
        return _legacy_ref_basename(value)
    return _HIDDEN_LEGACY_SUBAGENT_REF


# LLM: _legacy_ref_basename preserves the visible artifact name after removing old path prefixes.
# 函数用途: 从旧式路径里提取最后的文件名，避免模型继续看到 data/subagents 目录结构。
def _legacy_ref_basename(value: str) -> str:
    name = Path(str(value).replace("\\", "/")).name
    return name or _HIDDEN_LEGACY_SUBAGENT_REF


# LLM: sanitize_model_visible_tool_output repairs old archived internal tool outputs at read time.
# 函数用途: 只净化内部编排/状态工具的模型可见正文；普通文件、网页、命令输出保持原文。
def sanitize_model_visible_tool_output(tool: str, output: str) -> str:
    if not is_model_visible_internal_tool(tool):
        return output
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        sanitized = sanitize_model_visible_refs(output)
        return sanitized if isinstance(sanitized, str) else output
    return json.dumps(sanitize_model_visible_refs(value), ensure_ascii=False, indent=2)


# LLM: is_model_visible_internal_tool names framework tools whose outputs should not expose legacy refs.
# 函数用途: 判断一个工具是否属于内部编排/状态展示面；这个列表不是业务文件类型白名单。
def is_model_visible_internal_tool(tool: str) -> bool:
    return str(tool or "").strip() in _INTERNAL_MODEL_VISIBLE_TOOLS


# LLM: is_legacy_subagent_path recognizes old work-order refs without blocking current home paths.
# 函数用途: 判断字符串是否指向旧式 data/subagents 运行目录；普通业务文本和新 ~/.my-agent 路径不受影响。
def is_legacy_subagent_path(value: str) -> bool:
    text = str(value or "").replace("\\", "/")
    return "/data/subagents/" in text or text.startswith("data/subagents/")


__all__ = [
    "is_legacy_subagent_path",
    "is_model_visible_internal_tool",
    "sanitize_model_visible_refs",
    "sanitize_model_visible_tool_output",
]
