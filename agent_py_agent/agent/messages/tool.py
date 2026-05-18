from __future__ import annotations

from .models import MessageCard, MessageTarget
from .store import MessageStore


class MessageTool:
    def __init__(self, store: MessageStore):
        self.store = store

    def send_message(
        self,
        *,
        sender: MessageTarget,
        target: MessageTarget,
        content: str,
        task_id: str | None = None,
        message_type: str = "text",
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> MessageCard:
        return self.store.send(
            sender=sender,
            target=target,
            content=content,
            task_id=task_id,
            message_type=message_type,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    def read_inbox(self, target: MessageTarget, *, unread_only: bool = True) -> list[MessageCard]:
        return self.store.read_inbox(target, unread_only=unread_only)

    def ack_message(self, target: MessageTarget, message_id: str) -> bool:
        return self.store.ack(target, message_id)

    def broadcast_progress(self, *, task_id: str, route_target: MessageTarget, content: str) -> MessageCard:
        return self.send_message(
            sender=MessageTarget(kind="task", identifier=task_id),
            target=route_target,
            content=content,
            task_id=task_id,
            message_type="progress",
            idempotency_key=f"{task_id}:progress:{content}",
            metadata={"task_id": task_id},
        )

    def send_need_user_input(self, *, task_id: str, session_id: str, prompt: str) -> MessageCard:
        return self.send_message(
            sender=MessageTarget(kind="task", identifier=task_id),
            target=MessageTarget(kind="session", identifier=session_id),
            content=prompt,
            task_id=task_id,
            message_type="need_user_input",
            idempotency_key=f"{task_id}:need_user_input:{prompt}",
            metadata={"task_id": task_id, "requires_frontstage_agent": True},
        )
