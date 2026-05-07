# LLM: Session runtime module; keep conversation state and persistence contracts stable.
# 模块用途: 维护会话运行时状态、上下文和持久化边界。

from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# LLM: Session 属于跨通道会话管理的类边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 类用途: 集中保存会话字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发会话归属、上下文同步和用户隔离相关副作用，需保持公开契约稳定。
@dataclass
class Session:

    session_id: str
    user_id: str
    created_at: float
    updated_at: float
    last_active_channel: str = "chat"
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: __post_init__ 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 发送init请求或消息，并把外部响应转换成内部可处理结果；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
    def __post_init__(self) -> None:
        """确保时间戳合理。"""
        if self.updated_at < self.created_at:
            self.updated_at = self.created_at

    # LLM: to_dict 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 转换dict的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def to_dict(self) -> dict[str, Any]:
        """转换为字典，用于序列化。"""
        return asdict(self)

    # LLM: from_dict 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 转换dict的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        """从字典创建会话实例。"""
        return cls(**data)

    # LLM: touch 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
    # 函数用途: 处理touch相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
    def touch(self, channel: str | None = None) -> None:
        """更新最后活跃时间。"""
        self.updated_at = time.time()
        if channel:
            self.last_active_channel = channel


# LLM: generate_session_id 属于跨通道会话管理的函数边界；调整时先确认会话归属、上下文同步和用户隔离仍按原契约工作。
# 函数用途: 处理generate会话id相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持会话归属、上下文同步和用户隔离上的返回值和副作用边界稳定。
def generate_session_id() -> str:
    timestamp = int(time.time())
    random_part = secrets.token_hex(4)  # 8 位十六进制 (32 bits)
    return f"sess_{timestamp}_{random_part}"


__all__ = ["Session", "generate_session_id"]
