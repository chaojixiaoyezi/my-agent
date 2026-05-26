# LLM: Repair-summary rendering is split out to keep orchestration live summaries below size limits.
# 模块用途: 渲染外置 dispatch 输出里的修复建议机器字段，让 root 能直接派 repair child。

from __future__ import annotations

import json
from typing import Any

_MAX_INLINE_JSON = 900
_MAX_TOOL_CALL_JSON = 2400


# LLM: repair_advice_action_lines keeps top-level repair calls copyable after output externalization.
# 函数用途: 顶层 repair_advice 聚合在 records 外；这里渲染失败 refs 和建议工具调用，避免被普通摘要截断。
def repair_advice_action_lines(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    lines = ["- repair_advice: available"]
    if value.get("failed_run_ids"):
        lines.append(f"- repair_failed_run_ids: {_json_inline(value.get('failed_run_ids'))}")
    if value.get("failure_refs"):
        lines.append(f"- repair_failure_refs: {_json_inline(value.get('failure_refs'))}")
    suggested = value.get("suggested_tool_call")
    if isinstance(suggested, dict):
        lines.append(f"- repair_next_tool: {suggested.get('tool', '')}")
        lines.append(f"- repair_suggested_tool_call: {_json_inline(suggested, limit=_MAX_TOOL_CALL_JSON)}")
    return lines


# LLM: _json_inline bounds nested advisory JSON while keeping suggested tool calls mostly copyable.
# 函数用途: 将修复建议中的 dict/list 压成单行 JSON；超长时保留稳定截断提示。
def _json_inline(value: object, *, limit: int = _MAX_INLINE_JSON) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return _clip(text, limit=limit)


# LLM: _clip is intentionally local so repair summary does not depend on the larger reducer module.
# 函数用途: 给修复建议摘要做统一长度保护，避免长 goal 重新撑爆 live prompt。
def _clip(value: Any, *, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + f"...[truncated {len(text) - limit} chars]"
