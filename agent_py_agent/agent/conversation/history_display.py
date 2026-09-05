# LLM: 本模块只把同 owner/thread 已保存的会话消息投影为公开显示事件；不读路径、不执行工具、不改变模型历史。
# 模块用途: 给 Gateway 和本地会话恢复共用完整正文、思考与工具的显示转换，问答预览不再代替正文。

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from .background_transcript import public_background_transcript_text
from .models import is_audit_background_transcript_entry
from .native_history import canonical_native_messages_from_metadata

HISTORY_DISPLAY_SCHEMA = "conversation_history_display.v1"


# LLM: 调用方已解析 owner/thread 并限制读取窗口；本投影不得再用模型预览的回合数截断后台消息组。
# 函数用途: 显示所读取窗口的全部公开消息；后台 commentary 不是独立用户回合，未完成输入也保留。
def conversation_history_display_events(
    rows: Sequence[object],
) -> tuple[dict[str, object], ...]:
    groups: dict[str, list[object]] = {}
    for index, row in enumerate(rows):
        if is_audit_background_transcript_entry(row):
            continue
        if getattr(row, "role", "") not in {"user", "assistant"}:
            continue
        metadata = getattr(row, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        identity = str(
            metadata.get("conversation_request_id")
            or metadata.get("gateway_request_id")
            or metadata.get("agent_attempt_id")
            or getattr(row, "message_id", "")
            or f"row-{index}"
        )
        groups.setdefault(identity, []).append(row)
    events: list[dict[str, object]] = []
    for identity in groups:
        events.extend(_turn_events(identity, groups[identity]))
    return tuple(events)


# LLM: 真实 user 只来自 canonical row；native user 的文本通常是内部 runtime 注入，不能公开或当成用户输入。
# 函数用途: 恢复一轮的输入和已保存 native 内容，无 native 时保留全部正式 commentary/final。
def _turn_events(identity: str, rows: list[object]) -> list[dict[str, object]]:
    request_id = f"history:{identity}"
    events = [
        _event(request_id, f"user:{index}", "user_message", {"text": _public(row.content)})
        for index, row in enumerate(rows)
        if row.role == "user" and getattr(row, "content", "")
    ]
    assistants = [row for row in rows if row.role == "assistant"]
    native = next((
        messages for row in reversed(assistants)
        if (messages := canonical_native_messages_from_metadata(getattr(row, "metadata", None)))
    ), ())
    if not native:
        events.extend(
            _event(request_id, f"assistant:{index}", "assistant_completed", {"text": _public(row.content)})
            for index, row in enumerate(assistants)
            if getattr(row, "content", "")
        )
        return events
    # Compact 可能只留下本轮尾部 native；此前正式 commentary 仍在 raw rows 中，不能因选择 native 而隐藏。
    native_prose_count = sum(
        message.get("role") == "assistant" and any(
            block.get("type") == "text" and block.get("text")
            for block in _blocks(message)
        )
        for message in native
    )
    earlier_prose = assistants[:max(0, len(assistants) - native_prose_count)]
    events.extend(
        _event(request_id, f"earlier:{index}", "assistant_completed", {"text": _public(row.content)})
        for index, row in enumerate(earlier_prose)
        if getattr(row, "content", "")
    )
    final = next((
        row for row in reversed(assistants)
        if str(getattr(row, "metadata", {}).get("assistant_part_id") or "final") == "final"
    ), None)
    events.extend(_native_events(
        request_id, native, final_text=_public(final.content) if final is not None else None,
    ))
    return events


# LLM: 原生类型是唯一映射依据；tool_result 按精确 id 配对；末次正文用 canonical final，签名/注入/参数不透传。
# 函数用途: 按原顺序重放灰色思考、正文与工具结果；缺失结果明确显示未知，绝不伪造成功。
def _native_events(
    request_id: str, messages: tuple[dict, ...], *, final_text: str | None,
) -> list[dict[str, object]]:
    results = {
        str(block.get("tool_use_id") or ""): block
        for message in messages if message.get("role") == "user"
        for block in _blocks(message)
        if block.get("type") == "tool_result" and block.get("tool_use_id")
    }
    events: list[dict[str, object]] = []
    seen_calls: set[str] = set()
    terminal_index = len(messages) - 1
    if any(block.get("type") == "tool_use" for block in _blocks(messages[-1])):
        terminal_index = -1
    assistant_blocks = (
        (index, block_index, block)
        for index, message in enumerate(messages) if message.get("role") == "assistant"
        for block_index, block in enumerate(_blocks(message))
    )
    for index, block_index, block in assistant_blocks:
        key = f"native:{index}:{block_index}"
        kind = block.get("type")
        if kind == "text" and index == terminal_index and final_text is not None:
            continue
        if kind in {"text", "thinking"}:
            text = _public(block.get("thinking" if kind == "thinking" else "text"))
            if text:
                events.append(_event(
                    request_id, key,
                    "thinking_completed" if kind == "thinking" else "assistant_completed",
                    {"text": text},
                ))
        elif kind == "tool_use":
            call_id = str(block.get("id") or "")
            if not call_id or call_id in seen_calls:
                continue
            seen_calls.add(call_id)
            events.append(_tool_event(request_id, key, block, results.get(call_id)))
    if final_text:
        events.append(_event(request_id, "final", "assistant_completed", {"text": final_text}))
    return events


# LLM: 工具终态仅由 provider-neutral is_error 布尔值表述，不从输出中的成功/失败字样反推副作用状态。
# 函数用途: 为已保存工具结果构建同款折叠卡；无结果或无状态时生成中性的历史缺口说明。
def _tool_event(
    request_id: str, key: str, call: Mapping, result: Mapping | None,
) -> dict[str, object]:
    name = _public(call.get("name"))
    if result is None or not isinstance(result.get("is_error"), bool):
        return _event(request_id, key, "system_message", {
            "text": f"{name}：已保存调用，这段历史没有完整的工具结果状态。",
        })
    content = result.get("content")
    if isinstance(content, list):
        content = "\n".join(
            str(block.get("text") or "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    elif not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    failed = result["is_error"]
    return _event(
        request_id, key, "tool_failed" if failed else "tool_completed",
        {"tool": name, "output": _public(content), "ok": not failed},
        phase="failed" if failed else "completed",
    )


# LLM: 内容块只读合法的 native schema；字符串 assistant 是公开正文，未知块保持未投影而非执行。
# 函数用途: 把原生消息的两种合法 content 形状统一成可遍历的类型块。
def _blocks(message: Mapping) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [block for block in content or [] if isinstance(block, dict)]


# LLM: 使用与现有 main/child transcript 相同的公开正文清洗；不截断普通回复，也不泄漏宿主绝对路径。
# 函数用途: 清洗供前端显示的字符串，不改变原账本内容。
def _public(value: object) -> str:
    return public_background_transcript_text(str(value or ""), limit=0)


# LLM: 展示 ID 来自持久回合身份与块位置，可幂等重放；此信封没有任何控制、执行或持久化权。
# 函数用途: 创建统一的恢复事件，供现有 TUI reducer 直接显示。
def _event(
    request_id: str, key: str, kind: str, payload: dict[str, object], *, phase: str = "completed",
) -> dict[str, object]:
    return {
        "schema": HISTORY_DISPLAY_SCHEMA, "request_id": request_id,
        "block_id": f"{request_id}:{key}", "kind": kind, "phase": phase, "payload": payload,
    }
