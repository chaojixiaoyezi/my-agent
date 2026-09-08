# LLM: 公开消息来自唯一 canonical transcript；前台绑定须明确，内部 Audit 继续过滤，不写第二份正文或改变模型历史。
# 模块用途: 在既有消息游标上传递前台用户输入/最终回复和后台已提交回复，供同会话各窗口显示。

from __future__ import annotations

from ..turn_end import turn_end_notice
from .history_display import (
    conversation_history_display_events,
    foreground_gateway_request_id,
    public_assistant_message_event,
)
from .models import is_audit_background_transcript_entry


# LLM: after 是 canonical 行偏移；只新增明确 Gateway 前台 user/final，不把无来源或内部消息扩为公开正文。
# 函数用途: 增量取得同会话已提交消息；前台带请求关联让原页去重，后台仍按独立消息显示。
def read_background_response_page(
    store: object, thread_id: str, *, after: int = 0, include_foreground: bool = False,
) -> tuple[list[dict], int, bool]:
    entries, cursor, errors = store.message_page_after_offset_report(thread_id, after=after)
    if errors:
        return [], after, False
    notices = []
    for entry in entries:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        gateway_id = foreground_gateway_request_id(entry) if include_foreground else ""
        user = entry.role == "user" and bool(gateway_id)
        final = entry.role == "assistant" and metadata.get("assistant_part_id") == "final"
        if is_audit_background_transcript_entry(entry) or not (
            user or final and (gateway_id or metadata.get("background_delivery_reason"))
        ):
            continue
        display_events = conversation_history_display_events([entry])
        if user and not display_events:
            continue
        event = display_events[0] if user else public_assistant_message_event(entry)
        notices.append({
            "schema_version": "background_message.v1",
            "notice_id": entry.message_id,
            "message_id": entry.message_id,
            "thread_id": entry.thread_id,
            "display_kind": "user_message" if user else "assistant_response",
            "content": event["payload"]["text"],
            "created_at": entry.created_at,
            **({"gateway_request_id": gateway_id} if gateway_id else {}),
            **({"display_events": list(display_events)}
               if gateway_id or any(item.get("covered_background_request_id") for item in display_events)
               or turn_end_notice(metadata.get("turn_end_reason")) else {}),
        })
    return notices, cursor, True
