# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..parsers.common import utc_now


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 Checkpoint 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 Checkpoint 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class Checkpoint:
    source_id: str
    cursor_kind: str
    cursor: dict[str, Any]
    last_committed_batch_id: str | None
    last_event_time: str | None
    updated_at: str

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "cursor_kind": self.cursor_kind,
            "cursor": self.cursor,
            "last_committed_batch_id": self.last_committed_batch_id,
            "last_event_time": self.last_event_time,
            "updated_at": self.updated_at,
        }


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 CheckpointCommit 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 CheckpointCommit 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class CheckpointCommit:
    source_id: str
    cursor_kind: str
    cursor: Mapping[str, Any]
    last_committed_batch_id: str
    last_event_time: str | None


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 CheckpointStore 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 CheckpointStore 的持久化入口，把路径、读写和查询操作集中到同一对象。
class CheckpointStore:
    """JSON checkpoint store scoped by source_id."""

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.checkpoints_dir = self.root / "checkpoints"

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 path_for 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 path for 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def path_for(self, source_id: str) -> Path:
        return self.checkpoints_dir / f"{safe_source_id(source_id)}.json"

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 load 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 load 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def load(self, source_id: str) -> dict[str, Any]:
        path = self.path_for(source_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 commit 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 commit 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def commit(
        self,
        *,
        params: CheckpointCommit | None = None,
        commit: CheckpointCommit | None = None,
        source_id: str = "",
        cursor_kind: str = "",
        cursor: Mapping[str, Any] | None = None,
        last_committed_batch_id: str = "",
        last_event_time: str | None = None,
    ) -> Checkpoint:
        item = params or commit or CheckpointCommit(
            source_id=str(source_id),
            cursor_kind=str(cursor_kind),
            cursor=cursor or {},
            last_committed_batch_id=str(last_committed_batch_id),
            last_event_time=last_event_time,
        )
        checkpoint = Checkpoint(
            source_id=item.source_id,
            cursor_kind=item.cursor_kind,
            cursor=dict(item.cursor),
            last_committed_batch_id=item.last_committed_batch_id,
            last_event_time=item.last_event_time,
            updated_at=utc_now(),
        )
        write_json_atomic(self.path_for(item.source_id), checkpoint.to_dict())
        return checkpoint


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 safe_source_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 safe source id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def safe_source_id(source_id: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_.-]+", "_", source_id.strip())
    return value or "unknown"


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 write_json_atomic 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write json atomic 相关记录，集中处理目标路径、格式化和状态更新。
def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
