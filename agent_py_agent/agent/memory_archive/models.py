from __future__ import annotations

"""LLM: DTOs for compression snapshots and raw memory archive events.

新手说明:
这个文件只定义“要存什么”，不负责“存到哪里”。可以把它理解成两张表的字段设计:
压缩前快照保存上下文压缩前必须保住的现场，冷归档事件保存用户、助手、工具、派工等流水。
以后要查“刚才发生了什么”或“压缩前状态是什么”，其他模块就按这些字段找。
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


def utc_now_iso() -> str:
    """LLM: return a timezone-aware UTC timestamp for archive records.

    新手说明:
    如果调用方没有传时间，我们就用当前 UTC 时间补上。
    这样跨地区、跨机器看日志时，时间不会因为本地时区不同而乱掉。

    返回说明:
    返回 ISO 8601 字符串，例如 `2026-04-30T12:00:00+00:00`。
    """

    return datetime.now(timezone.utc).isoformat()


@dataclass
class CompressionSnapshot:
    """LLM: structured pre-compression snapshot used as the recovery anchor.

    新手说明:
    这是一张“压缩前拍照”。
    比如上下文快满了，系统准备把聊天压短，在压之前先把任务状态、工具调用、下一步、token 估算等关键现场写下来。
    模型之后如果压缩漂了，可以回头读这张快照恢复。

    字段说明:
    `snapshot_id` 是这张快照自己的唯一编号；`session_id` 是会话编号；
    `compression_id` 是一次压缩动作的编号；`turn_range` 描述快照覆盖的轮次范围。
    `participants` 记录参与者；`user_intents` 记录用户想做什么；
    `assistant_actions` 记录助手已经做了什么；`tool_calls` 记录工具调用摘要；
    `dispatch_events` 记录子代理/派工事件；`task_refs` 记录相关任务或文件引用。
    `decisions` 是已经做出的判断；`open_questions` 是还没解决的问题；
    `next_actions` 是恢复后应该继续做的事；`token_usage` 保存 token 估算；
    `archive_level` 表示归档详细程度；`content_paths` 指向更长正文或证据文件；
    `created_at` 是快照创建时间，默认使用 UTC 当前时间。
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
    turn_id: str = ""
    role: str = "system"
    content: str = ""
    token_estimate: int = 0
    timestamp: str = ""
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        """LLM: keep legacy created_at and explicit timestamp fields synchronized.

        新手说明:
        老代码主要看 `created_at`，新压缩链路更喜欢 `timestamp`。
        这里把两个字段保持同步，避免查询层和测试层看到两套时间语义。
        """

        if not self.timestamp:
            self.timestamp = self.created_at
        if not self.created_at:
            self.created_at = self.timestamp or utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        """LLM: serialize this snapshot into a JSON-ready mapping.

        新手说明:
        写 JSONL 前先把 dataclass 变成普通 dict。
        这样后面追加字段也比较稳，测试和 readback 验证也都用同一种形状。

        返回说明:
        返回可以直接交给 `json.dumps()` 或 JSONL 写入函数的字典。
        """

        return asdict(self)


@dataclass
class RawMemoryEvent:
    """LLM: structured cold-archive event for searchable conversation recovery.

    新手说明:
    这是一条“原始会话流水”的索引卡。
    它不一定保存完整正文，但会保存谁说的、对谁说、做了什么、工具是否成功、任务 ID 是什么、正文在哪里等字段。
    以后用户说“刚刚那个工具失败在哪”，就能按这些字段找回来。

    字段说明:
    `event_id` 是事件唯一编号；`session_id` 是会话编号；
    `request_id` 和 `run_id` 用来把事件串回一次用户请求和一次运行。
    `speaker` 是发起方，`target` 是接收方，`action` 是动作类型；
    `created_at` 是事件时间；`status` 和 `error_code` 记录是否成功以及错误码。
    `is_dispatch` 标记这是不是派工事件；`task_id` 关联任务；
    `tool_name`、`tool_call_id`、`tool_success` 描述工具调用。
    `content_preview` 保存短预览；`content_path` 指向长正文；
    `content_hash` 用来校验正文是否变化；`visibility` 控制可见性；
    `source` 说明事件来自 run、chat、CLI 还是测试夹具。
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
    archive_level: int = 3

    def to_dict(self) -> dict[str, Any]:
        """LLM: serialize this raw archive event into a JSON-ready mapping.

        新手说明:
        冷归档是按行写 JSON。
        这个方法保证每条事件都用稳定字段输出，后面做检索、迁移、审计时不会每个调用点各写各的格式。

        返回说明:
        返回可以直接写入 raw archive JSONL 的普通字典。
        """

        return asdict(self)
