"""有界外发回执索引：只定位 canonical 消息，不授予会话或资料权限。"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ..common.json_io import locked_json_path, write_private_json_file_atomic_unlocked


class OutboundMessageIndex:
    """按消息 ID 分片；单次只读写一片，30 日过期，片内最多 2000 条。"""

    ttl_seconds = 30 * 86400
    max_entries = 2000
    max_shard_bytes = 16 * 1024 * 1024

    def __init__(self, owner_home: str | Path):
        self.root = Path(owner_home) / "data" / "outbound_messages"

    def _path(self, message_id: str) -> Path:
        return self.root / (hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:2] + ".json")

    @staticmethod
    def _timestamp(value: object) -> float:
        if not isinstance(value, str):
            raise ValueError("外发记录时间无效")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("外发记录时间缺少时区")
        return parsed.timestamp()

    @classmethod
    def _validate(cls, entry: object) -> dict:
        if not isinstance(entry, Mapping) or entry.get("schema_version") != "outbound_message.v1":
            raise ValueError("外发记录结构无效")
        fields = ("channel", "channel_message_id", "chat_id", "thread_id", "conversation_message_id")
        if any(not isinstance(entry.get(key), str) or not entry[key] or len(entry[key].encode()) > 2048 for key in fields):
            raise ValueError("外发记录身份无效")
        refs = entry.get("evidence_refs")
        if (not isinstance(refs, list) or len(refs) > 32
                or any(not isinstance(ref, str) or len(ref.encode()) > 4096 for ref in refs)):
            raise ValueError("外发记录证据引用无效")
        for key in ("task_id", "workflow_run_id", "receipt_id"):
            if entry.get(key) is not None and (not isinstance(entry[key], str) or len(entry[key].encode()) > 2048):
                raise ValueError("外发记录关联无效")
        cls._timestamp(entry.get("sent_at"))
        return {key: entry.get(key) for key in ("schema_version", *fields, "task_id", "workflow_run_id",
                                               "evidence_refs", "receipt_id", "sent_at")}

    def _read(self, path: Path) -> list[dict]:
        try:
            with path.open("rb") as stream:
                raw = stream.read(self.max_shard_bytes + 1)
        except FileNotFoundError:
            return []
        if len(raw) > self.max_shard_bytes:
            raise ValueError("外发索引分片超出读取预算")
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get("schema_version") != "outbound_message_index.v1"
                or not isinstance(value.get("entries"), list) or len(value["entries"]) > self.max_entries):
            raise ValueError("外发索引分片无效")
        return [self._validate(row) for row in value["entries"]]

    @staticmethod
    def _key(row: dict) -> tuple[str, str, str]:
        return row["channel"], row["chat_id"], row["channel_message_id"]

    def record(self, entry: dict, *, now: float | None = None) -> dict:
        """同片读改写共用原进程/线程锁；重放不刷新 TTL，也不允许改绑正文。"""
        row = self._validate(entry)
        current = time.time() if now is None else now
        if current - self._timestamp(row["sent_at"]) >= self.ttl_seconds:
            return row
        path = self._path(row["channel_message_id"])
        with locked_json_path(path):
            entries = {self._key(item): item for item in self._read(path)
                       if current - self._timestamp(item["sent_at"]) < self.ttl_seconds}
            key = self._key(row)
            previous = entries.get(key)
            if previous is not None:
                if previous != row:
                    raise ValueError("外发回执已绑定其它 canonical 消息")
                return previous
            entries[key] = row
            retained = sorted(entries.values(), key=lambda item: (self._timestamp(item["sent_at"]), self._key(item)))[-self.max_entries:]
            payload = {"schema_version": "outbound_message_index.v1", "entries": retained}
            if len(json.dumps(payload, ensure_ascii=False).encode()) > self.max_shard_bytes:
                raise ValueError("外发索引分片超出写入预算")
            write_private_json_file_atomic_unlocked(path, payload)
        return row

    def lookup(self, channel_message_id: str, *, channel: str, chat_id: str, now: float | None = None) -> dict | None:
        """只读一片；未知结构、损坏及过期均不作为可用引用，不创建目录或锁。"""
        if not all(isinstance(value, str) and value and len(value.encode()) <= 2048
                   for value in (channel_message_id, channel, chat_id)):
            return None
        current = time.time() if now is None else now
        try:
            entries = self._read(self._path(channel_message_id))
            matches = [row for row in entries if self._key(row) == (channel, chat_id, channel_message_id)
                       and current - self._timestamp(row["sent_at"]) < self.ttl_seconds]
        except (OSError, ValueError, TypeError):
            return None
        return matches[0] if len(matches) == 1 else None
