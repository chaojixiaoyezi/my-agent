from __future__ import annotations

"""LLM: DTOs for compression snapshots and raw memory archive events.

给人看的解释：
这个文件只定义“要存什么”，不负责“存到哪里”。
压缩前快照记录一轮上下文压缩前必须保住的现场；冷归档事件记录用户、助手、工具、派工等流水，方便以后按字段找回。
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


def utc_now_iso() -> str:
    """LLM: return a timezone-aware UTC timestamp for archive records.

    给人看的解释：
    如果调用方没有传时间，我们就用当前 UTC 时间补上。
    这样跨地区、跨机器看日志时，时间不会因为本地时区不同而乱掉。
    """

    return datetime.now(timezone.utc).isoformat()


@dataclass
class CompressionSnapshot:
    """LLM: structured pre-compression snapshot used as the recovery anchor.

    给人看的解释：
    这是一张“压缩前拍照”。
    比如上下文快满了，系统准备把聊天压短，在压之前先把任务状态、工具调用、下一步、token 估算等关键现场写下来。
    模型之后如果压缩漂了，可以回头读这张快照恢复。
    """

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
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """LLM: serialize this snapshot into a JSON-ready mapping.

        给人看的解释：
        写 JSONL 前先把 dataclass 变成普通 dict。
        这样后面追加字段也比较稳，测试和 readback 验证也都用同一种形状。
        """

        return asdict(self)


@dataclass
class RawMemoryEvent:
    """LLM: structured cold-archive event for searchable conversation recovery.

    给人看的解释：
    这是一条“原始会话流水”的索引卡。
    它不一定保存完整正文，但会保存谁说的、对谁说、做了什么、工具是否成功、任务 ID 是什么、正文在哪里等字段。
    以后用户说“刚刚那个工具失败在哪”，就能按这些字段找回来。
    """

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
    content_preview: str = ""
    content_path: str = ""
    content_hash: str = ""
    visibility: str = "private"
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """LLM: serialize this raw archive event into a JSON-ready mapping.

        给人看的解释：
        冷归档是按行写 JSON。
        这个方法保证每条事件都用稳定字段输出，后面做检索、迁移、审计时不会每个调用点各写各的格式。
        """

        return asdict(self)
