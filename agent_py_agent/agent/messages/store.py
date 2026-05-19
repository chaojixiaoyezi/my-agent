from __future__ import annotations

# LLM: MessageStore persists internal messages, inbox deliveries, and externalized large bodies.
# 模块用途: 提供内部消息的发送、读取、确认、幂等投递和大段正文外部化存储。
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from ..gateway_parts.io import read_json_file, write_json_file_atomic
from .models import DeliveryCard, MessageCard, MessageTarget, new_message_id


 # LLM: MessageStore owns file-backed inboxes without calling external channels.
 # 类用途: 管理 MessageCard、DeliveryCard、幂等索引和超长正文文件。
class MessageStore:
    # LLM: MessageStore.__init__ defines folders and inline body budget.
    # 函数用途: 初始化消息存储根目录、收件箱目录和正文内联上限。
    def __init__(self, root: str | Path, *, max_inline_chars: int = 8000):
        self.root = Path(root)
        self.messages_dir = self.root / "messages"
        self.inboxes_dir = self.root / "inboxes"
        self.idempotency_dir = self.root / "idempotency"
        self.bodies_dir = self.root / "bodies"
        self.max_inline_chars = max_inline_chars

    # LLM: MessageStore.send creates one message and one target delivery atomically enough for local runtime use.
    # 函数用途: 发送内部消息，写入消息文件、目标 inbox 和幂等索引。
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
            with _file_lock(self.root / "locks" / f"idempotency-{_safe_name(idempotency_key)}.lock"):
                return self._send_locked(
                    sender=sender,
                    target=target,
                    content=content,
                    message_type=message_type,
                    task_id=task_id,
                    idempotency_key=idempotency_key,
                    metadata=metadata,
                )
        return self._send_locked(
            sender=sender,
            target=target,
            content=content,
            message_type=message_type,
            task_id=task_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    # LLM: MessageStore._send_locked writes one message after optional idempotency locking.
    # 函数用途: 保持普通发送和幂等发送共用同一落盘逻辑。
    def _send_locked(
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

    # LLM: MessageStore.get_message loads a persisted message by id.
    # 函数用途: 根据 message_id 读取 MessageCard，不存在时抛出 KeyError。
    def get_message(self, message_id: str) -> MessageCard:
        payload = read_json_file(self._message_path(message_id))
        if not payload:
            raise KeyError(message_id)
        return MessageCard.from_dict(payload)

    # LLM: MessageStore.read_message_body retrieves externalized content when present.
    # 函数用途: 读取消息完整正文；若未外部化则返回内联正文。
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

    # LLM: MessageStore.read_inbox returns messages visible to a structured target.
    # 函数用途: 读取某个 target 的 inbox，可选择只读未读消息。
    def read_inbox(self, target: MessageTarget, *, unread_only: bool = True) -> list[MessageCard]:
        inbox_dir = self._inbox_dir(target)
        try:
            paths = sorted(inbox_dir.glob("*.json"))
        except OSError:
            return []
        messages: list[tuple[float, float, str, MessageCard]] = []
        for path in paths:
            delivery = DeliveryCard.from_dict(read_json_file(path))
            if unread_only and delivery.status == "read":
                continue
            try:
                message = self.get_message(delivery.message_id)
            except KeyError:
                continue
            messages.append((delivery.created_at, message.created_at, message.message_id, message))
        messages.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in messages]

    # LLM: MessageStore.ack marks a delivery read without deleting history.
    # 函数用途: 确认某个目标已读指定消息。
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

    # LLM: MessageStore._read_idempotency maps a dedupe key back to the first message.
    # 函数用途: 根据幂等 key 查询已创建的 message_id。
    def _read_idempotency(self, key: str) -> str | None:
        payload = read_json_file(self._idempotency_path(key))
        return str(payload.get("message_id")) if payload.get("message_id") else None

    # LLM: MessageStore._prepare_content externalizes large bodies to protect the main chain.
    # 函数用途: 按内联上限处理消息正文，超长时写入 body 文件并返回预览和引用。
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

    # LLM: MessageStore._message_path centralizes message snapshot paths.
    # 函数用途: 生成 MessageCard 文件路径。
    def _message_path(self, message_id: str) -> Path:
        return self.messages_dir / f"{message_id}.json"

    # LLM: MessageStore._inbox_dir centralizes per-target inbox folders.
    # 函数用途: 生成某个消息目标的 inbox 目录。
    def _inbox_dir(self, target: MessageTarget) -> Path:
        return self.inboxes_dir / _safe_name(target.key)

    # LLM: MessageStore._delivery_path centralizes delivery file paths.
    # 函数用途: 生成某个目标下指定消息的投递记录路径。
    def _delivery_path(self, target: MessageTarget, message_id: str) -> Path:
        return self._inbox_dir(target) / f"{message_id}.json"

    # LLM: MessageStore._idempotency_path centralizes dedupe key files.
    # 函数用途: 生成幂等 key 对应的索引文件路径。
    def _idempotency_path(self, key: str) -> Path:
        return self.idempotency_dir / f"{_safe_name(key)}.json"


 # LLM: _safe_name converts target/id strings into filesystem-safe names.
 # 函数用途: 把消息目标或 key 转成可用作文件名的字符串。
def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in ("-", "_", ".") else "_" for char in value)


# LLM: _file_lock gives local message idempotency a small cross-thread/process guard without dependencies.
# 函数用途: 用 O_EXCL lock 文件保护幂等索引读写，避免并发发送同 key 产生多条消息。
@contextmanager
def _file_lock(path: Path, *, timeout_seconds: float = 5.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"timed out waiting for lock: {path}") from None
            time.sleep(0.005)
    try:
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
