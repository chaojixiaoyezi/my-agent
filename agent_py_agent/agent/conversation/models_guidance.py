# LLM: GuidanceEntry is the durable soft-steering row for runtime agent hints.
# 模块用途: 定义运行中补充提示的账本结构，独立于 thread/message 基础模型避免大文件膨胀。

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# LLM: GuidanceEntry is a one-shot soft hint row for a target agent/thread/task.
# 类用途: 保存运行中补充提示的目标、正文、优先级、投递时间和元数据。
@dataclass(frozen=True)
class GuidanceEntry:
    guidance_id: str
    target_type: str
    target_id: str
    message: str
    sender: str = ""
    priority: str = "normal"
    delivery: str = "next_turn"
    created_at: float = 0.0
    delivered_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict serializes guidance rows for JSONL storage.
    # 函数用途: 将 GuidanceEntry 转成普通字典，供 append_jsonl 写入。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # LLM: from_dict tolerates old or partial guidance rows.
    # 函数用途: 从 JSON 行恢复 GuidanceEntry，缺失字段使用安全默认值。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuidanceEntry:
        metadata = data.get("metadata")
        return cls(
            guidance_id=str(data.get("guidance_id") or ""),
            target_type=str(data.get("target_type") or ""),
            target_id=str(data.get("target_id") or ""),
            message=str(data.get("message") or ""),
            sender=str(data.get("sender") or ""),
            priority=str(data.get("priority") or "normal"),
            delivery=str(data.get("delivery") or "next_turn"),
            created_at=float(data.get("created_at") or 0.0),
            delivered_at=float(data.get("delivered_at") or 0.0),
            metadata=metadata if isinstance(metadata, dict) else {},
        )
