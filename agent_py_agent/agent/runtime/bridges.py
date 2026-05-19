from __future__ import annotations

# LLM: Runtime bridges map existing gateway/session/notification facts into Card and Message contracts.
# 模块用途: 提供适配器消息、通知、gateway 响应和 task_registry 记录到新 runtime 合同的薄转换。
from pathlib import Path
from typing import Any

from ..adapter.protocol import IncomingMessage
from ..cards import TaskCard, TaskStatus
from ..messages import MessageCard, MessageStore, MessageTarget, MessageTool
from ..notification.models import Notification


 # LLM: incoming_to_message normalizes external adapter input into an internal inbox message.
 # 函数用途: 把 IncomingMessage 写入目标 session 的内部消息收件箱。
def incoming_to_message(incoming: IncomingMessage, *, messages_root: str | Path) -> MessageCard:
    session_id = str(incoming.metadata.get("session_id") or incoming.metadata.get("active_session_id") or incoming.user_id)
    tool = MessageTool(MessageStore(messages_root))
    return tool.send_message(
        sender=MessageTarget(kind="channel", identifier=f"{incoming.channel}:{incoming.user_id}"),
        target=MessageTarget(kind="session", identifier=session_id),
        content=incoming.content,
        message_type="incoming",
        idempotency_key=f"incoming:{incoming.channel}:{incoming.message_id}",
        metadata={
            "channel": incoming.channel,
            "user_id": incoming.user_id,
            "external_message_id": incoming.message_id,
            **incoming.metadata,
        },
    )


 # LLM: notification_to_message preserves notification delivery as an idempotent internal message.
 # 函数用途: 把 Notification 转为发往原 session 的内部通知消息。
def notification_to_message(notification: Notification, *, messages_root: str | Path) -> MessageCard:
    tool = MessageTool(MessageStore(messages_root))
    return tool.send_message(
        sender=MessageTarget(kind="task", identifier=notification.task_id),
        target=MessageTarget(kind="session", identifier=notification.session_id),
        content=notification.message,
        task_id=notification.task_id,
        message_type="notification",
        idempotency_key=f"notification:{notification.notification_id}",
        metadata={
            "notification_id": notification.notification_id,
            "user_id": notification.user_id,
            "channel": notification.channel,
            "status": notification.status,
        },
    )


 # LLM: gateway_response_to_message turns gateway results into frontstage-visible messages.
 # 函数用途: 把 gateway response 转为 session inbox 中的结果消息。
def gateway_response_to_message(response: dict[str, Any], *, messages_root: str | Path) -> MessageCard:
    metadata = dict(response.get("metadata") or {})
    task_id = str(metadata.get("task_id") or response.get("task_id") or "")
    session_id = str(metadata.get("session_id") or response.get("session_id") or "")
    if not session_id:
        session_id = str(response.get("user_id") or "admin")
    content = str(response.get("response") or response.get("error") or "")
    tool = MessageTool(MessageStore(messages_root))
    return tool.send_message(
        sender=MessageTarget(kind="gateway", identifier=str(response.get("id", "unknown"))),
        target=MessageTarget(kind="session", identifier=session_id),
        content=content,
        task_id=task_id or None,
        message_type="gateway_result",
        idempotency_key=f"gateway:{response.get('id', '')}:result",
        metadata={
            "gateway_request_id": response.get("id", ""),
            "ok": bool(response.get("ok")),
            "status": response.get("status", ""),
            **metadata,
        },
    )


 # LLM: task_registry_record_to_card projects existing SQLite task rows into TaskCard snapshots.
 # 函数用途: 把 task_registry 查询结果转换为 TaskCard，不修改原 schema。
def task_registry_record_to_card(record: dict[str, Any]) -> TaskCard:
    created_at = float(record.get("created_at") or 0.0)
    updated_at = float(record.get("updated_at") or created_at)
    return TaskCard(
        task_id=str(record.get("task_id", "")),
        session_id=str(record.get("session_id", "")),
        user_id=str(record.get("user_id", "")),
        status=TaskStatus(str(record.get("status", TaskStatus.QUEUED))),
        goal=str(record.get("goal", "")),
        created_at=created_at,
        updated_at=updated_at,
        metadata={"source": "task_registry"},
    )
