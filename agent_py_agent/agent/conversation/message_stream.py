# LLM: 公开消息来自唯一 canonical transcript；前台绑定须明确，内部 Audit 继续过滤，不写第二份正文或改变模型历史。
# 模块用途: 同一消息游标传递已提交正文及可选的完整过程检查点；旧客户端跳过新类型，不重跑任务。

from __future__ import annotations

from dataclasses import dataclass

from ..turn_end import turn_end_notice
from .background_history import background_display_turn_from_row
from .display_checkpoint import display_checkpoint_event, is_display_checkpoint
from .history_display import (
    conversation_history_display_events,
    foreground_gateway_request_id,
    public_assistant_message_event,
)
from .models import is_audit_background_transcript_entry


# LLM: 客户端显示能力是一个不可变协议快照，只控制公开投影，不授予运行或审批权限。
# 类用途: 一起传递正文、前台流和过程检查点支持情况，避免HTTP/冷恢复各自多组开关失配。
@dataclass(frozen=True)
class NoticeDisplayCapabilities:
    foreground_messages: bool = False
    foreground_transcript: bool = False
    display_checkpoints: bool = False

    # LLM: wire能力必须是显式布尔true；字符串或数字不能隐式升级协议，无I/O或状态变更。
    # 函数用途: 从客户端声明读取三项显示能力，缺少字段就保持旧协议。
    @classmethod
    def from_payload(cls, value: object) -> NoticeDisplayCapabilities:
        fields = value if isinstance(value, dict) else {}
        return cls(*(fields.get(key) is True for key in ("foreground_messages", "foreground_transcript", "display_checkpoints")))


# LLM: canonical行游标覆盖所有类型；检查点需单独协商且只发终态，完整final快照覆盖本页同片检查点。
# 函数用途: 取得同会话正文和可选过程记录；前台仍需明确同意，不发送开始占位或伪造助手空回复。
def read_background_response_page(
    store: object, thread_id: str, *, after: int = 0, include_foreground: bool = False,
    include_display_checkpoints: bool = False,
) -> tuple[list[dict], int, bool]:
    entries, cursor, errors = store.message_page_after_offset_report(thread_id, after=after)
    if errors:
        return [], after, False
    notices = []
    covered = {snapshot["request_id"] for entry in entries if (snapshot := background_display_turn_from_row(entry))}
    for entry in entries:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        gateway_id = foreground_gateway_request_id(entry) if include_foreground else ""
        if is_display_checkpoint(entry):
            if include_display_checkpoints and (include_foreground or not foreground_gateway_request_id(entry)):
                event = display_checkpoint_event(entry, live=True)
                if event is not None and event["request_id"] not in covered:
                    notices.append({
                        "schema_version": "background_message.v1", "display_kind": "process_event", "content": "",
                        "notice_id": entry.message_id, "message_id": entry.message_id, "thread_id": entry.thread_id,
                        "created_at": entry.created_at, "display_events": [event],
                        **({"gateway_request_id": gateway_id} if gateway_id else {}),
                    })
            continue
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
