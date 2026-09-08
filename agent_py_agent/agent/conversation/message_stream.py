# LLM: 后台正文和结束提示来自唯一 canonical transcript；只分页投影，不写第二份回复、调用模型或改变状态。
# 模块用途: 为本地和 Gateway 的实时显示提供同一消息游标，完整保留后台快照及 typed 长度提示。

from __future__ import annotations

from ..turn_end import turn_end_notice
from .history_display import conversation_history_display_events, public_assistant_message_event
from .models import is_audit_background_transcript_entry


# LLM: after 是完整行偏移；已提交 final 投影快照和结束提示，覆盖标记不依赖末事件位置，不回灌模型。
# 函数用途: 增量取得后台回复及截断提示；相同文本不同消息可区分，错误保留原游标供重试。
def read_background_response_page(store: object, thread_id: str, *, after: int = 0) -> tuple[list[dict], int, bool]:
    entries, cursor, errors = store.message_page_after_offset_report(thread_id, after=after)
    if errors:
        return [], after, False
    notices = []
    for entry in entries:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        if (
            entry.role != "assistant"
            or not metadata.get("background_delivery_reason")
            or metadata.get("assistant_part_id") != "final"
            or is_audit_background_transcript_entry(entry)
        ):
            continue
        event = public_assistant_message_event(entry)
        display_events = conversation_history_display_events([entry])
        notices.append({
            "schema_version": "background_message.v1",
            "notice_id": entry.message_id,
            "message_id": entry.message_id,
            "thread_id": entry.thread_id,
            "display_kind": "assistant_response",
            "content": event["payload"]["text"],
            "created_at": entry.created_at,
            **({"display_events": list(display_events)}
               if any(item.get("covered_background_request_id") for item in display_events)
               or turn_end_notice(metadata.get("turn_end_reason")) else {}),
        })
    return notices, cursor, True
