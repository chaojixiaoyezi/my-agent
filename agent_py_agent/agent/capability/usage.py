from __future__ import annotations

import json

# LLM: Capability usage records turn tool/skill choices into routing feedback.
# 模块用途: 记录 capability 使用原因和结果，供后续路由排序参考。
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "capability_usage_record.v1"


# LLM: CapabilityUsageRecord is append-only feedback, not an authorization record.
# 类用途: 保存一次能力选择、执行结果和验收反馈。
@dataclass(frozen=True)
class CapabilityUsageRecord:
    capability_id: str
    task_fingerprint: str
    selected_reason: str
    accepted: bool
    failure_code: str = ""
    created_at: float = 0.0
    metadata: dict[str, object] = field(default_factory=dict)

    # LLM: CapabilityUsageRecord.with_timestamp makes records append-ready without mutating them.
    # 函数用途: 为使用记录补创建时间，已有时间时保持原记录不变。
    def with_timestamp(self) -> CapabilityUsageRecord:
        if self.created_at:
            return self
        return CapabilityUsageRecord(
            capability_id=self.capability_id,
            task_fingerprint=self.task_fingerprint,
            selected_reason=self.selected_reason,
            accepted=self.accepted,
            failure_code=self.failure_code,
            created_at=time.time(),
            metadata=dict(self.metadata),
        )

    # LLM: CapabilityUsageRecord.to_payload writes the durable JSONL schema for usage feedback.
    # 函数用途: 把使用记录序列化成带 schema_version 的持久化对象。
    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "capability_id": self.capability_id,
            "task_fingerprint": self.task_fingerprint,
            "selected_reason": self.selected_reason,
            "accepted": self.accepted,
            "failure_code": self.failure_code,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    # LLM: CapabilityUsageRecord.from_payload validates persisted usage feedback before routing uses it.
    # 函数用途: 从 JSONL payload 恢复使用记录，坏 schema 或缺字段时明确失败。
    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CapabilityUsageRecord:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported capability usage schema")
        capability_id = str(payload.get("capability_id") or "").strip()
        task_fingerprint = str(payload.get("task_fingerprint") or "").strip()
        selected_reason = str(payload.get("selected_reason") or "").strip()
        if not capability_id or not task_fingerprint:
            raise ValueError("capability usage record requires capability_id and task_fingerprint")
        metadata = payload.get("metadata")
        return cls(
            capability_id=capability_id,
            task_fingerprint=task_fingerprint,
            selected_reason=selected_reason,
            accepted=bool(payload.get("accepted")),
            failure_code=str(payload.get("failure_code") or ""),
            created_at=_float(payload.get("created_at")),
            metadata=metadata if isinstance(metadata, dict) else {},
        )


# LLM: CapabilityUsageStats is a compact scoring input derived from records.
# 类用途: 汇总某个 capability 的成功/失败次数和最近失败代码。
@dataclass(frozen=True)
class CapabilityUsageStats:
    capability_id: str
    successes: int = 0
    failures: int = 0
    last_failure_code: str = ""

    # LLM: CapabilityUsageStats.score_bonus turns accepted/rejected history into a small routing signal.
    # 函数用途: 计算能力路由排序的历史反馈加减分。
    @property
    def score_bonus(self) -> float:
        return float(self.successes * 2 - self.failures * 3)


# LLM: CapabilityUsageStore keeps a small in-process record list; persistence can wrap it later.
# 类用途: 提供记录和统计能力使用效果的最小存储接口。
class CapabilityUsageStore:
    # LLM: CapabilityUsageStore.__init__ replays optional records through the same stamp path.
    # 函数用途: 初始化内存使用记录列表，并接收已有记录作为种子数据。
    def __init__(self, records: list[CapabilityUsageRecord] | None = None):
        self._records: list[CapabilityUsageRecord] = []
        for record in records or []:
            self.record(record)

    # LLM: CapabilityUsageStore.record appends one feedback record and returns the stored version.
    # 函数用途: 记录一次 capability 使用结果，缺时间时自动补时间。
    def record(self, record: CapabilityUsageRecord) -> CapabilityUsageRecord:
        stamped = record.with_timestamp()
        self._records.append(stamped)
        return stamped

    # LLM: CapabilityUsageStore.records returns a defensive snapshot for diagnostics and tests.
    # 函数用途: 返回当前使用记录副本，避免调用方直接修改内部列表。
    def records(self) -> list[CapabilityUsageRecord]:
        return list(self._records)

    # LLM: CapabilityUsageStore.stats_for summarizes feedback for one capability id.
    # 函数用途: 统计某个 capability 的成功失败次数和最近失败代码。
    def stats_for(self, capability_id: str) -> CapabilityUsageStats:
        successes = 0
        failures = 0
        last_failure_code = ""
        for record in self._records:
            if record.capability_id != capability_id:
                continue
            if record.accepted:
                successes += 1
            else:
                failures += 1
                last_failure_code = record.failure_code
        return CapabilityUsageStats(
            capability_id=capability_id,
            successes=successes,
            failures=failures,
            last_failure_code=last_failure_code,
        )


# LLM: JsonlCapabilityUsageStore gives routing feedback durable memory without adding services.
# 类用途: 用 JSONL 追加保存 capability 使用记录，启动时忽略损坏行。
class JsonlCapabilityUsageStore(CapabilityUsageStore):
    # LLM: JsonlCapabilityUsageStore.__init__ loads valid JSONL records without rewriting the file.
    # 函数用途: 初始化 JSONL 使用记录存储，启动时跳过损坏行。
    def __init__(self, path: Path):
        self.path = path
        self._records: list[CapabilityUsageRecord] = []
        for record in _load_records(path):
            CapabilityUsageStore.record(self, record)

    # LLM: JsonlCapabilityUsageStore.record appends usage feedback durably after in-memory storage.
    # 函数用途: 记录一次 capability 使用结果，并追加写入 JSONL 文件。
    def record(self, record: CapabilityUsageRecord) -> CapabilityUsageRecord:
        stamped = super().record(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stamped.to_payload(), ensure_ascii=False, sort_keys=True) + "\n")
        return stamped


# LLM: _load_records is intentionally tolerant so one bad JSONL line does not erase learning history.
# 函数用途: 从 JSONL 文件加载有效使用记录，并忽略无法解析的行。
def _load_records(path: Path) -> list[CapabilityUsageRecord]:
    if not path.exists():
        return []
    records: list[CapabilityUsageRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(CapabilityUsageRecord.from_payload(payload))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return records


# LLM: _float keeps persisted timestamps best-effort and non-fatal.
# 函数用途: 把外部 payload 的时间字段转为 float，失败时回退为 0。
def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
