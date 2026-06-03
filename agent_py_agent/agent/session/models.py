
from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Session:

    session_id: str
    user_id: str
    created_at: float
    updated_at: float
    last_active_channel: str = "chat"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """确保时间戳合理。"""
        if self.updated_at < self.created_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于序列化。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        """从字典创建会话实例。"""
        return cls(**data)

    def touch(self, channel: str | None = None) -> None:
        """更新最后活跃时间。"""
        self.updated_at = time.time()
        if channel:
            self.last_active_channel = channel


def generate_session_id() -> str:
    timestamp = int(time.time())
    random_part = secrets.token_hex(4)  # 8 位十六进制 (32 bits)
    return f"sess_{timestamp}_{random_part}"


__all__ = ["Session", "generate_session_id"]
