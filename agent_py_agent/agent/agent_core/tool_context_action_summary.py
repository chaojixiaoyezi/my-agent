# LLM: Tool action summaries preserve machine recovery fields when large outputs are externalized.
# 模块用途: 大工具输出被外置后，提取错误码、建议动作和可复制工具调用，避免 preview 截断关键修复字段。

from __future__ import annotations

import json
from typing import Any

_MAX_INLINE_JSON = 1600
_TOP_LEVEL_KEYS = (
    "action",
    "status",
    "session_id",
    "target_path",
    "manifest_path",
    "code",
    "message",
    "error_code",
    "recommended_action",
    "retryable",
)
_TOOL_CALL_SUFFIX = "_tool_call"


# LLM: actionable_tool_result_summary renders structured facts only; it never parses model prose.
# 函数用途: 从工具 JSON 输出抽取恢复动作，保证外置后模型仍能看到下一步机器合同。
def actionable_tool_result_summary(result: object, archive_record: dict[str, object]) -> str:
    payload = _json_object(str(getattr(result, "output", "") or ""))
    if not payload:
        return ""
    action_fields = _action_fields(payload)
    if not action_fields and not _has_error_fact(result, payload):
        return ""
    lines = [
        f"[tool={getattr(result, 'tool', '')}; status={'ok' if getattr(result, 'ok', False) else 'error'}]",
        "actionable_tool_result:",
        "- policy: full tool output is archived; live prompt keeps structured recovery facts.",
    ]
    lines.extend(_top_level_lines(payload, result))
    lines.extend(_artifact_integrity_lines(payload.get("artifact_integrity")))
    lines.extend(_tool_call_lines(action_fields))
    lines.extend(_archive_lines(archive_record))
    return "\n".join(lines)


# LLM: _action_fields collects any top-level suggested tool call without hard-coding one tool.
# 函数用途: 提取 reset/finish/abort/continue/suggested 等顶层工具调用字段。
def _action_fields(payload: dict[str, Any]) -> dict[str, object]:
    return {
        key: value
        for key, value in payload.items()
        if key.endswith(_TOOL_CALL_SUFFIX) and isinstance(value, dict)
    }


# LLM: _has_error_fact decides whether a compact action summary is needed.
# 函数用途: 检查结果和 envelope 是否带错误、修复动作或 artifact_integrity 结构事实。
def _has_error_fact(result: object, payload: dict[str, Any]) -> bool:
    return bool(
        not getattr(result, "ok", True)
        or payload.get("error_code")
        or payload.get("recommended_action")
        or payload.get("artifact_integrity")
    )


# LLM: _top_level_lines renders top-level machine fields for prompt recovery.
# 函数用途: 把 error_code/recommended_action 等结构字段转成短上下文行。
def _top_level_lines(payload: dict[str, Any], result: object) -> list[str]:
    lines: list[str] = []
    for key in _TOP_LEVEL_KEYS:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"- {key}: {_json_inline(value)}")
    error_code = str(getattr(result, "error_code", "") or "")
    if error_code and not payload.get("error_code"):
        lines.append(f"- error_code: {error_code}")
    recommended = str(getattr(result, "recommended_action", "") or "")
    if recommended and not payload.get("recommended_action"):
        lines.append(f"- recommended_action: {recommended}")
    return lines


# LLM: _artifact_integrity_lines summarizes artifact integrity fields.
# 函数用途: 输出 blocker/warning/issue code，不把产物正文塞回 prompt。
def _artifact_integrity_lines(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    lines = ["- artifact_integrity: available"]
    for key in ("blocker_codes", "warning_codes", "ok"):
        if key in value:
            lines.append(f"- artifact_integrity_{key}: {_json_inline(value.get(key))}")
    issue_codes = _issue_codes(value.get("issues"))
    if issue_codes:
        lines.append(f"- artifact_integrity_issue_codes: {_json_inline(issue_codes)}")
    return lines


# LLM: _issue_codes extracts compact artifact issue codes.
# 函数用途: 从 artifact_integrity.issues 列表中取 code 字段，限制数量防止上下文膨胀。
def _issue_codes(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    codes: list[str] = []
    for item in value[:8]:
        if isinstance(item, dict) and item.get("code"):
            codes.append(str(item.get("code")))
    return codes


# LLM: _tool_call_lines renders suggested tool call fields in stable key order.
# 函数用途: 把 reset/abort/continue 等结构化工具调用字段写成短行。
def _tool_call_lines(fields: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for key in sorted(fields):
        lines.append(f"- {key}: {_json_inline(fields[key])}")
    return lines


# LLM: _archive_lines renders output artifact refs from the archive record.
# 函数用途: 给下一轮模型提供 output_path/artifact_ref/hash 等结构化恢复锚点。
def _archive_lines(record: dict[str, object]) -> list[str]:
    return [
        f"- output_path: {record.get('output_path', '')}",
        f"- output_artifact_ref: {record.get('artifact_ref') or record.get('output_path') or ''}",
        f"- output_call_id: {record.get('id', '')}",
        f"- output_scoped_call_id: {record.get('scoped_call_id', '')}",
        f"- output_hash: {record.get('output_hash', '')}",
    ]


# LLM: _json_object parses JSON output envelopes without fallback prose heuristics.
# 函数用途: 只接受合法 JSON object，解析失败返回 None。
def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# LLM: _json_inline renders bounded JSON snippets for live prompt context.
# 函数用途: 将结构化值压成单行，超过预算时截断并保留标记。
def _json_inline(value: object) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return text if len(text) <= _MAX_INLINE_JSON else text[:_MAX_INLINE_JSON].rstrip() + "...[truncated]"
