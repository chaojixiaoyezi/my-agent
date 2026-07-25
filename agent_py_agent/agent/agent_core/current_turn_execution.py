from __future__ import annotations

import json
from typing import Any

_MAX_RECENT_CALLS = 32
_MAX_MUTATING_CALLS = 64
_MAX_REFS_PER_CALL = 4
_MUTATING_EFFECTS = frozenset({"mutating", "dangerous"})


def render_current_turn_execution_facts(
    agent: object,
    records: list[dict[str, object]] | None,
) -> str:
    """Render the current request's structured tool facts at the prompt tail.

    会话运行时 keeps function-call outputs as typed response items and 长期助手 warns
    that prose is only a self-report until a returned handle is verified.  Our
    provider prompt is a single user message on the first native-tool round, so
    the compact tool catalog can be far from the active user request.  Put one
    bounded projection of the authoritative runtime records *after* that
    request.  This does not infer intent or parse assistant prose.
    """

    normalized = [
        _execution_call(agent, record)
        for record in list(records or [])
        if isinstance(record, dict)
    ]
    mutating = [item for item in normalized if item["effect"] in _MUTATING_EFFECTS]
    successful_mutating = [item for item in mutating if item["ok"] is True]
    unsuccessful_mutating = [item for item in mutating if item["ok"] is not True]
    payload = {
        "schema": "current_turn_execution.v1",
        "scope": "current_request_only",
        "call_count": len(normalized),
        "successful_mutating_calls": successful_mutating[-_MAX_MUTATING_CALLS:],
        "unsuccessful_mutating_calls": unsuccessful_mutating[-_MAX_MUTATING_CALLS:],
        "recent_calls": normalized[-_MAX_RECENT_CALLS:],
        "omitted_call_count": max(0, len(normalized) - _MAX_RECENT_CALLS),
    }
    return (
        "# Current Turn Execution Facts\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n```\n"
        "这是本次请求的结构化执行事实，不是历史对话或模型自述。"
        "只有 successful_mutating_calls 中列出的本轮成功调用，才允许支持"
        "“已经保存、修改、发送、创建或删除”等副作用结论；空列表表示本轮尚无这类成功事实。"
        "unsuccessful_mutating_calls 必须按失败或未完成说明。"
        "若成功调用给出 refs，扩大成功结论前优先按句柄回读核验。"
    )


def _execution_call(agent: object, record: dict[str, object]) -> dict[str, object]:
    tool = str(record.get("tool") or "").strip() or "unknown"
    item: dict[str, object] = {
        "call_id": str(record.get("call_id") or record.get("id") or "").strip(),
        "tool": tool,
        "effect": _tool_effect(agent, tool),
        "ok": record.get("ok") is True,
        "status": str(record.get("status") or "").strip(),
        "handler_executed": record.get("handler_executed") is True,
    }
    for key in (
        "error_code",
        "failure_stage",
        "effect_outcome",
        "tool_operation_status",
    ):
        value = str(record.get(key) or "").strip()
        if value:
            item[key] = value
    refs = _record_refs(record)
    if refs:
        item["refs"] = refs
    return item


def _tool_effect(agent: object, tool_name: str) -> str:
    registry = getattr(agent, "tools", None)
    tools = getattr(registry, "tools", None)
    tool = tools.get(tool_name) if isinstance(tools, dict) else None
    effect = str(getattr(getattr(tool, "spec", None), "effect", "") or "").strip().lower()
    return effect or "unknown"


def _record_refs(record: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for key in (
        "effect_source_ref",
        "artifact_ref",
        "source_artifact_ref",
        "output_path",
    ):
        _append_ref(refs, record.get(key))
    nested = record.get("tool_result_refs")
    if isinstance(nested, list):
        for item in nested:
            if not isinstance(item, dict):
                continue
            for key in ("url", "path", "id", "source_ref", "artifact_ref"):
                _append_ref(refs, item.get(key))
    envelope = record.get("tool_result_envelope")
    if isinstance(envelope, dict):
        for key in ("url", "path", "target_path", "output_path", "source_ref", "artifact_ref"):
            _append_ref(refs, envelope.get(key))
    return refs[:_MAX_REFS_PER_CALL]


def _append_ref(refs: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in refs:
        refs.append(text)


__all__ = ["render_current_turn_execution_facts"]
