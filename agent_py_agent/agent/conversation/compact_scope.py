# LLM: Compact 作用域是 checkpoint 和当前请求共用的只读身份；不得从摘要正文推断，
# 也不得把任务或活动轮的局部摘要提升为全线程状态。
# 模块用途: 严格保存 Compact 摘要的线程、任务或活动轮范围，并裁决摘要能否继承。
from __future__ import annotations

import math
from dataclasses import dataclass


# LLM: 字段整体组成任务身份；活动轮也保留附带任务字段，只有完全相同的活动轮身份可互用。
# 类用途: 表示一个冻结的摘要作用域，序列化时不接受未知或缺失字段。
@dataclass(frozen=True)
class CompactScope:
    kind: str = "thread"
    task_id: str = ""
    turn_id: str = ""
    task_ids: tuple[str, ...] = ()
    anchor_message_id: str = ""
    created_at: float = 0.0

    # LLM: 构造与账本读取共用同一校验，不得宽松转换坏身份或默许全线程携带局部字段。
    # 函数用途: 拒绝不完整和相互矛盾的范围事实，避免坏 checkpoint 扩大历史读取。
    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in {"thread", "task", "turn"}:
            raise ValueError("Compact 作用域类型无效")
        if type(self.task_ids) is not tuple or any(type(value) is not str for value in (
            self.task_id, self.turn_id, self.anchor_message_id, *self.task_ids,
        )):
            raise ValueError("Compact 作用域身份类型无效")
        if len(set(self.task_ids)) != len(self.task_ids) or any(not value for value in self.task_ids):
            raise ValueError("Compact 任务范围含重复或空身份")
        if type(self.created_at) not in {int, float} or not math.isfinite(self.created_at) or self.created_at < 0:
            raise ValueError("Compact 作用域创建时间无效")
        if self.kind == "thread" and any((
            self.task_id, self.turn_id, self.task_ids, self.anchor_message_id, self.created_at,
        )):
            raise ValueError("全线程 Compact 不得带局部身份")
        if self.kind == "task" and (not self.task_id or self.turn_id or not self.created_at):
            raise ValueError("任务 Compact 缺少身份或创建时间")
        if self.kind == "turn" and (not self.turn_id or self.created_at or self.anchor_message_id or self.task_ids):
            raise ValueError("活动轮 Compact 身份无效")

    # LLM: 完整字段共同组成持久身份；调用方必须把这个字典与 checkpoint 一同纳入候选封印。
    # 函数用途: 输出固定字段集，供 checkpoint 写入和读取时逐字段比较。
    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "task_ids": list(self.task_ids),
            "anchor_message_id": self.anchor_message_id,
            "created_at": self.created_at,
        }

    # LLM: 不从缺失字段猜默认值，也不容忍未知字段；旧 checkpoint 应由显式版本迁移读取。
    # 函数用途: 从严格固定的账本字典恢复作用域，不修改原数据。
    @classmethod
    def from_dict(cls, data: object) -> CompactScope:
        fields = {"kind", "task_id", "turn_id", "task_ids", "anchor_message_id", "created_at"}
        if not isinstance(data, dict) or set(data) != fields or type(data["task_ids"]) is not list:
            raise ValueError("Compact 作用域字段不完整或含未知字段")
        if any(type(item) is not str for item in data["task_ids"]):
            raise ValueError("Compact 任务身份类型无效")
        return cls(
            kind=data["kind"],
            task_id=data["task_id"],
            turn_id=data["turn_id"],
            task_ids=tuple(data["task_ids"]),
            anchor_message_id=data["anchor_message_id"],
            created_at=data["created_at"],
        )


THREAD_COMPACT_SCOPE = CompactScope()


# LLM: 继承只按结构化身份；任务可读取创建边界前的全线程摘要，活动轮从不继承全线程。
# 函数用途: 判断候选摘要对当前请求是否适用，供 writer 基础检查与只读 resolver 共用。
def scope_can_inherit(
    scope: CompactScope,
    candidate_scope: CompactScope,
    created_at: float,
) -> bool:
    if scope == candidate_scope:
        return True
    if scope.kind != "task" or candidate_scope != THREAD_COMPACT_SCOPE:
        return False
    return (
        type(created_at) in {int, float}
        and math.isfinite(created_at)
        and 0 < created_at <= scope.created_at
    )


__all__ = ["CompactScope", "THREAD_COMPACT_SCOPE", "scope_can_inherit"]
