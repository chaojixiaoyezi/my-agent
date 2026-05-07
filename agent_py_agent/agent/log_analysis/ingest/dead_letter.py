# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...file_io import append_jsonl
from ..parsers.common import sha256_text, utc_now
from .checkpoint import safe_source_id


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 DeadLetterRef 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DeadLetterRef 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class DeadLetterRef:
    path: str
    count: int


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 DeadLetterRecord 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 DeadLetterRecord 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class DeadLetterRecord:
    # LLM: Dead-letter writes accept this record bundle instead of open kwargs.
    reason: str
    raw_ref: str
    line_no: int | None = None
    raw_line: str | None = None
    raw_fields: Mapping[str, Any] | None = None
    parser_id: str | None = None


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 DeadLetterWriter 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 DeadLetterWriter 的状态和协作方法，作为当前模块对外复用的领域对象。
class DeadLetterWriter:
    """Append-only dead-letter writer for malformed ingest records."""

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, root: str | Path, *, source_id: str, batch_id: str):
        self.root = Path(root)
        self.source_id = source_id
        self.batch_id = batch_id
        safe_source = safe_source_id(source_id)
        self.path = self.root / "dead_letter" / safe_source / f"{batch_id}.jsonl"
        self.diagnostic_path = self.root / "events" / "dead_letter_diagnostics.jsonl"
        self.count = 0

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 write 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 write 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def write(
        self,
        *,
        params: DeadLetterRecord | None = None,
        record: DeadLetterRecord | None = None,
        reason: str = "",
        raw_ref: str = "",
        line_no: int | None = None,
        raw_line: str | None = None,
        raw_fields: Mapping[str, Any] | None = None,
        parser_id: str | None = None,
    ) -> None:
        if params is None:
            params = record or DeadLetterRecord(
                reason=str(reason),
                raw_ref=str(raw_ref),
                line_no=line_no,
                raw_line=raw_line,
                raw_fields=raw_fields,
                parser_id=parser_id,
            )
        now = utc_now()
        payload = self._payload(params, now)
        append_jsonl(self.path, payload, sort_keys=True)
        append_jsonl(self.diagnostic_path, self._diagnostic(params, now), sort_keys=True)
        self.count += 1

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _payload 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 payload 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _payload(self, params: DeadLetterRecord, now: str) -> dict[str, Any]:
        return {
            "dead_letter_id": f"dlq-{sha256_text(f'{self.batch_id}:{params.raw_ref}:{params.reason}')[:24]}",
            "batch_id": self.batch_id,
            "source_id": self.source_id,
            "parser_id": params.parser_id,
            "reason": params.reason,
            "raw_ref": params.raw_ref,
            "line_no": params.line_no,
            "raw_line_preview": _preview(params.raw_line),
            "raw_line_sha256": f"sha256:{sha256_text(params.raw_line or '')}",
            "raw_fields": dict(params.raw_fields or {}),
            "created_at": now,
        }

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _diagnostic 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 diagnostic 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def _diagnostic(self, params: DeadLetterRecord, now: str) -> dict[str, Any]:
        return {
            "event_type": "log_parse_failure",
            "batch_id": self.batch_id,
            "source_id": self.source_id,
            "raw_ref": params.raw_ref,
            "line_no": params.line_no,
            "reason": params.reason,
            "dead_letter_path": str(self.path),
            "created_at": now,
        }

    # LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 refs 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 refs 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
    def refs(self) -> list[dict[str, Any]]:
        if self.count == 0:
            return []
        return [{"path": str(self.path), "count": self.count}]


# LLM: 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态；修改 _preview 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 preview 在当前模块中的核心转换或协调步骤，衔接 日志摄取流程解析原始事件并维护 checkpoint、去重和 dead-letter 状态。
def _preview(value: str | None, *, max_chars: int = 2048) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:max_chars]
