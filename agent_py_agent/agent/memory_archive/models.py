
from __future__ import annotations

"""DTOs for compression snapshots and raw memory archive events.

新手说明:
这个文件只定义'要存什么'，不负责'存到哪里'。可以把它理解成两张表的字段设计:
压缩前快照保存上下文压缩前必须保住的现场，冷归档事件保存用户、助手、工具、派工等流水。
以后要查'刚才发生了什么'或'压缩前状态是什么'，其他模块就按这些字段找。
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


def utc_now_iso() -> str:

    return datetime.now(timezone.utc).isoformat()


@dataclass
class CompressionSnapshot:

    snapshot_id: str
    session_id: str
    compression_id: str
    turn_range: JsonValue
    participants: list[str] = field(default_factory=list)
    user_intents: list[str] = field(default_factory=list)
    assistant_actions: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    dispatch_events: list[dict[str, Any]] = field(default_factory=list)
    task_refs: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    archive_level: int = 3
    content_paths: list[str] = field(default_factory=list)
    turn_id: str = ""
    role: str = "system"
    content: str = ""
    token_estimate: int = 0
    timestamp: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:

        if not self.timestamp:
            self.timestamp = self.created_at
        if not self.created_at:
            self.created_at = self.timestamp or utc_now_iso()

    def to_dict(self) -> dict[str, Any]:

        return asdict(self)


# LLM: RawMemoryEvent is the append-only archive contract. New execution facts must remain
# optional for old rows and round-trip through to_dict without converting prose into authority.
# 类用途: 保存一条原始会话或工具事件，供后续查询、压缩和记忆证据核验。
@dataclass
class RawMemoryEvent:

    event_id: str
    session_id: str
    request_id: str = ""
    run_id: str = ""
    speaker: str = ""
    target: str = ""
    action: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    status: str = ""
    error_code: str = ""
    is_dispatch: bool = False
    task_id: str = ""
    tool_name: str = ""
    tool_call_id: str = ""
    tool_success: bool | None = None
    operation_id: str = ""
    effect_outcome: str = ""
    source_ref: str = ""
    content_preview: str = ""
    content_path: str = ""
    content_hash: str = ""
    visibility: str = "private"
    source: str = ""
    archive_level: int = 3

    def to_dict(self) -> dict[str, Any]:

        return asdict(self)
