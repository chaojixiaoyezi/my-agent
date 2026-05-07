# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 utc_now_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc now iso 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def utc_now_iso() -> str:

    return datetime.now(timezone.utc).isoformat()


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 CompressionSnapshot 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CompressionSnapshot 的字段集合，在模块边界间传递结构化状态和结果。
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

    # LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 __post_init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 post init 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
    def __post_init__(self) -> None:

        if not self.timestamp:
            self.timestamp = self.created_at
        if not self.created_at:
            self.created_at = self.timestamp or utc_now_iso()

    # LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:

        return asdict(self)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 RawMemoryEvent 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 RawMemoryEvent 的字段集合，在模块边界间传递结构化状态和结果。
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
    content_preview: str = ""
    content_path: str = ""
    content_hash: str = ""
    visibility: str = "private"
    source: str = ""
    archive_level: int = 3

    # LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:

        return asdict(self)
