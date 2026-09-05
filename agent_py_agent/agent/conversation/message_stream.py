# LLM: 后台正文来自唯一 canonical transcript；本模块只分页读取与投影，不写第二份回复、调用模型或改变状态。
# 模块用途: 为本地和 Gateway 的实时显示提供同一消息游标，替代重复的 notices 正文文件。

from __future__ import annotations

from .history_display import public_assistant_message_event
from .models import is_audit_background_transcript_entry


# LLM: after 是完整 JSONL 行后的偏移，不是墙钟时间；所有读过的消息都推进游标，只有已提交后台 final 投影正文。
# 函数用途: 从同一会话增量取得后台回复；保持相同文本不同消息可区分，错误时保留原游标供重试。
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
        notices.append({
            "schema_version": "background_message.v1",
            "notice_id": entry.message_id,
            "message_id": entry.message_id,
            "thread_id": entry.thread_id,
            "display_kind": "assistant_response",
            "content": event["payload"]["text"],
            "created_at": entry.created_at,
        })
    return notices, cursor, True
