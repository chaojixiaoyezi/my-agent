from __future__ import annotations

import json
import time
from pathlib import Path

from ..gateway_parts.io import read_json_file, write_json_file_atomic
from .models import DeliveryCard, MessageCard, MessageTarget, new_message_id


class MessageStore:
    def __init__(self, root: str | Path, *, max_inline_chars: int = 8000):
        self.root = Path(root)
        self.messages_dir = self.root / "messages"
        self.inboxes_dir = self.root / "inboxes"
        self.idempotency_dir = self.root / "idempotency"
        self.bodies_dir = self.root / "bodies"
        self.max_inline_chars = max_inline_chars

    def send(
        self,
        *,
        sender: MessageTarget,
        target: MessageTarget,
        content: str,
        message_type: str = "text",
        task_id: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> MessageCard:
        if idempotency_key:
            existing_id = self._read_idempotency(idempotency_key)
            if existing_id:
                return self.get_message(existing_id)

        message_id = new_message_id()
        visible_content, merged_metadata = self._prepare_content(message_id, content, metadata)
        message = MessageCard(
            message_id=message_id,
            sender=sender.key,
            target=target.key,
            content=visible_content,
            message_type=message_type,
            task_id=task_id,
            idempotency_key=idempotency_key,
            metadata=merged_metadata,
        )
        delivery = DeliveryCard(
            delivery_id=new_message_id("delivery"),
            message_id=message.message_id,
            target=target.key,
            idempotency_key=idempotency_key,
        )
        write_json_file_atomic(self._message_path(message.message_id), message.to_dict())
        write_json_file_atomic(self._delivery_path(target, message.message_id), delivery.to_dict())
        if idempotency_key:
            write_json_file_atomic(self._idempotency_path(idempotency_key), {"message_id": message.message_id})
        return message

    def get_message(self, message_id: str) -> MessageCard:
        payload = read_json_file(self._message_path(message_id))
        if not payload:
            raise KeyError(message_id)
        return MessageCard.from_dict(payload)

    def read_message_body(self, message: MessageCard) -> str:
        if not message.metadata.get("content_externalized"):
            return message.content
        ref = str(message.metadata.get("content_ref") or "")
        if not ref.startswith("message-body:"):
            return message.content
        body_id = ref.removeprefix("message-body:")
        try:
            return (self.bodies_dir / f"{_safe_name(body_id)}.txt").read_text(encoding="utf-8")
        except OSError:
            return message.content

    def read_inbox(self, target: MessageTarget, *, unread_only: bool = True) -> list[MessageCard]:
        inbox_dir = self._inbox_dir(target)
        try:
            paths = sorted(inbox_dir.glob("*.json"))
        except OSError:
            return []
        messages: list[MessageCard] = []
        for path in paths:
            delivery = DeliveryCard.from_dict(read_json_file(path))
            if unread_only and delivery.status == "read":
                continue
            try:
                messages.append(self.get_message(delivery.message_id))
            except KeyError:
                continue
        return messages

    def ack(self, target: MessageTarget, message_id: str) -> bool:
        path = self._delivery_path(target, message_id)
        payload = read_json_file(path)
        if not payload:
            return False
        delivery = DeliveryCard.from_dict(payload)
        delivery.status = "read"
        delivery.read_at = time.time()
        write_json_file_atomic(path, delivery.to_dict())
        return True

    def _read_idempotency(self, key: str) -> str | None:
        payload = read_json_file(self._idempotency_path(key))
        return str(payload.get("message_id")) if payload.get("message_id") else None

    def _prepare_content(self, message_id: str, content: str, metadata: dict | None) -> tuple[str, dict]:
        merged = dict(metadata or {})
        if len(content) <= self.max_inline_chars:
            merged.setdefault("content_externalized", False)
            merged.setdefault("content_chars", len(content))
            return content, merged
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        body_path = self.bodies_dir / f"{_safe_name(message_id)}.txt"
        body_path.write_text(content, encoding="utf-8")
        merged.update(
            {
                "content_externalized": True,
                "content_chars": len(content),
                "content_ref": f"message-body:{message_id}",
            }
        )
        return content[: self.max_inline_chars], merged

    def _message_path(self, message_id: str) -> Path:
        return self.messages_dir / f"{message_id}.json"

    def _inbox_dir(self, target: MessageTarget) -> Path:
        return self.inboxes_dir / _safe_name(target.key)

    def _delivery_path(self, target: MessageTarget, message_id: str) -> Path:
        return self._inbox_dir(target) / f"{message_id}.json"

    def _idempotency_path(self, key: str) -> Path:
        return self.idempotency_dir / f"{_safe_name(key)}.json"


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in value)
