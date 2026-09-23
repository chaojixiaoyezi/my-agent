# LLM: 本模块只把同 owner/thread 已保存的会话及 typed turn-end 投影为显示事件；不执行工具、不改变模型历史。
# 模块用途: 恢复已提交正文及逐块过程检查点；不重复同片快照，不把未保存终态的工具当完成。

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ..turn_end import turn_end_notice
from .background_history import background_display_turn_from_row
from .background_transcript import public_background_transcript_text
from .display_checkpoint import DISPLAY_CHECKPOINT_ROLE, display_checkpoint_events
from .history_order import order_history_events
from .history_page import history_group_identity
from .models import is_audit_background_transcript_entry
from .native_history import canonical_native_messages_from_metadata

HISTORY_DISPLAY_SCHEMA = "conversation_history_display.v1"


# LLM: 同一 conversation turn 的 native 与投递快照归同一显示片，按精确ID关联；前台ID只供发起页去重。
# 函数用途: 按canonical来源投影并恢复跨片事件顺序，插话不能被分组挤到回复后；不改变运行状态。
def conversation_history_display_events(
    rows: Sequence[object],
) -> tuple[dict[str, object], ...]:
    delivery_groups = {
        (getattr(row, "thread_id", ""), str(row.metadata["conversation_request_id"])): history_group_identity(row)
        for row in rows
        if background_display_turn_from_row(row) and row.metadata.get("conversation_request_id")
    }
    groups: dict[str, list[object]] = {}
    for row in rows:
        if is_audit_background_transcript_entry(row):
            continue
        if getattr(row, "role", "") not in {"user", "assistant", DISPLAY_CHECKPOINT_ROLE}:
            continue
        metadata = getattr(row, "metadata", {})
        turn = str(metadata.get("conversation_request_id") or "") if isinstance(metadata, Mapping) else ""
        identity = delivery_groups.get((getattr(row, "thread_id", ""), turn), history_group_identity(row))
        groups.setdefault(identity, []).append(row)
    events: list[dict[str, object]] = []
    for identity in groups:
        group = groups[identity]
        gateway_id = next((value for row in group if (value := foreground_gateway_request_id(row))), "")
        if any(isinstance(getattr(row, "metadata", None), Mapping) and row.metadata.get("background_delivery_reason") for row in group):
            gateway_id = ""
        projected = [*_turn_events(identity, group), *_turn_end_events(group)]
        events.extend({**event, **({"gateway_request_id": gateway_id} if gateway_id else {})} for event in projected)
    return order_history_events(rows, events)


# LLM: 只读取宿主 metadata 的精确请求关联；后台续片虽可携带原编号，仍是独立应显示的工作片。
# 函数用途: 返回可用于前台显示去重的请求号；不猜旧消息、不从正文或 task 名推导。
def foreground_gateway_request_id(row: object) -> str:
    metadata = getattr(row, "metadata", None)
    if not isinstance(metadata, Mapping) or metadata.get("background_delivery_reason"):
        return ""
    value = metadata.get("gateway_request_id")
    return value.strip() if isinstance(value, str) else ""


# LLM: 结束提示只由 canonical final 的结构化 reason 产生；ID 与原消息绑定，重放不能新增模型输入或重复提示。
# 函数用途: 在半截回复后另列技术说明，主代理、子代理和后台历史用同一规则；不改写原正文。
def _turn_end_events(rows: list[object]) -> list[dict[str, object]]:
    events = []
    for row in rows:
        metadata = getattr(row, "metadata", {})
        if not isinstance(metadata, Mapping) or row.role != "assistant":
            continue
        if str(metadata.get("assistant_part_id") or "final") != "final":
            continue
        if notice := turn_end_notice(metadata.get("turn_end_reason")):
            events.append(_event(
                f"history:{row.thread_id}:{row.message_id}", "turn-end", "system_message",
                {"text": notice}, phase="failed",
            ))
    return events


# LLM: user/final 使用 canonical message ID；完整快照仅覆盖自己的检查点，无快照时优先公开检查点而非native副本。
# 函数用途: 恢复同片输入和逐块过程，保留崩溃前旧工作片；最终回复独立提交，不把模型中间快照当回复。
def _turn_events(identity: str, rows: list[object]) -> list[dict[str, object]]:
    request_id = f"history:{identity}"
    # LLM: 历史回放的宿主路径策略必须与实时出口一致，且以**这条记录落账时的通道**为权威事实；
    #   否则实时保留绝对路径、回放又砍成 basename，同一条消息在界面上前后不一致。
    # 函数用途: 取得本组历史记录所属通道（多条时取第一条非空）。
    channel = next(
        (
            str(getattr(row, "channel", "") or "").strip().lower()
            for row in rows
            if str(getattr(row, "channel", "") or "").strip()
        ),
        "",
    )
    events = [
        _event(request_id, f"user:{row.message_id}", "user_message", {"text": _public(row.content, channel)})
        for row in rows
        if row.role == "user" and getattr(row, "content", "")
    ]
    assistants = [row for row in rows if row.role == "assistant"]
    snapshots = [snapshot for row in assistants if (snapshot := background_display_turn_from_row(row))]
    checkpoints = display_checkpoint_events(rows, covered_requests={item["request_id"] for item in snapshots})
    events.extend(checkpoints)
    for row in reversed(assistants):
        if snapshot := background_display_turn_from_row(row):
            events.extend({**item, "schema": HISTORY_DISPLAY_SCHEMA} for item in snapshot["events"])
            final_event = public_assistant_message_event(row)
            final_event["covered_background_request_id"] = snapshot["request_id"]
            if replacement := snapshot.get("live_final_block_id"):
                final_event["replaces_live_block_id"] = replacement
            events.append(final_event)
            return events
    if checkpoints:
        events.extend(public_assistant_message_event(row) for row in assistants
                      if getattr(row, "metadata", {}).get("assistant_part_id") == "final" and row.content)
        return events
    native = next((
        messages for row in reversed(assistants)
        if (messages := canonical_native_messages_from_metadata(getattr(row, "metadata", None)))
    ), ())
    if not native:
        events.extend(
            public_assistant_message_event(row)
            for row in assistants
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
        _event(request_id, f"earlier:{index}", "assistant_completed", {"text": _public(row.content, channel)})
        for index, row in enumerate(earlier_prose)
        if getattr(row, "content", "")
    )
    final = next((
        row for row in reversed(assistants)
        if str(getattr(row, "metadata", {}).get("assistant_part_id") or "final") == "final"
    ), None)
    events.extend(_native_events(
        request_id, native, channel=channel,
        final_text=_public(final.content, channel) if final is not None else None,
    ))
    if final is not None and final.content:
        events.append(public_assistant_message_event(final))
    return events


# LLM: canonical message_id 与 thread_id 是历史恢复和实时后台 final 共用的显示身份，不从正文或时间生成编号。
# 函数用途: 将一条已提交助手消息转换成稳定显示块，同一消息重放不再插入第二条。
def public_assistant_message_event(row: object) -> dict[str, object]:
    request_id = f"history:{row.thread_id}:{row.message_id}"
    return _event(request_id, "assistant", "assistant_completed", {
        "text": _public(row.content, getattr(row, "channel", "")),
    })


# LLM: 原生类型是唯一映射依据；tool_result 按精确 id 配对；跳过由调用方追加的 canonical final，签名/注入/参数不透传。
# 函数用途: 按原顺序重放灰色思考、正文与工具结果；缺失结果明确显示未知，绝不伪造成功。
def _native_events(
    request_id: str, messages: tuple[dict, ...], *, channel: object = "", final_text: str | None,
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
            text = _public(block.get("thinking" if kind == "thinking" else "text"), channel)
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
            events.append(_tool_event(request_id, key, block, results.get(call_id), channel))
    return events


# LLM: 工具终态仅由 provider-neutral is_error 布尔值表述，不从输出中的成功/失败字样反推副作用状态。
# 函数用途: 为已保存工具结果构建同款折叠卡；无结果或无状态时生成中性的历史缺口说明。
def _tool_event(
    request_id: str, key: str, call: Mapping, result: Mapping | None, channel: object = "",
) -> dict[str, object]:
    name = _public(call.get("name"), channel)
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
        {"tool": name, "output": _public(content, channel), "ok": not failed},
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
def _public(value: object, channel: object = "") -> str:
    return public_background_transcript_text(str(value or ""), limit=0, channel=channel)


# LLM: 展示 ID 来自持久回合身份与块位置，可幂等重放；此信封没有任何控制、执行或持久化权。
# 函数用途: 创建统一的恢复事件，供现有 TUI reducer 直接显示。
def _event(
    request_id: str, key: str, kind: str, payload: dict[str, object], *, phase: str = "completed",
) -> dict[str, object]:
    return {
        "schema": HISTORY_DISPLAY_SCHEMA, "request_id": request_id,
        "block_id": f"{request_id}:{key}", "kind": kind, "phase": phase, "payload": payload,
    }
