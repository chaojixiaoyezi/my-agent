from __future__ import annotations

"""Memory Curator 无正文运行审计。"""

# LLM: Run audit records lifecycle/count/cursor facts only; prompts, responses, daily summaries,
# candidate bodies, and transaction backups never enter this ledger.
# 模块用途: 为成功、失败和崩溃回滚生成幂等的 memory/curator/runs 日分片记录。

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic_unlocked,
)
from .candidate_models import normalize_iso_time, utc_now_iso
from .curator_models import CURATOR_RUN_SCHEMA_VERSION, CURATOR_TRIGGER_REASONS

_RUN_AUDIT_STATUSES = frozenset({"succeeded", "failed", "recovered_rollback"})
_RUN_AUDIT_PHASES = frozenset({"final", "recovery"})


# LLM: A record object replaces a many-parameter helper and keeps recovery provenance explicit
# without exposing user content.
# 类用途: 表示一次 Curator 最终结果或一次事务恢复审计事件。
@dataclass(frozen=True)
class CuratorRunRecord:
    run_id: str
    lease_id: str
    status: str
    reason: str
    provider: str
    model: str
    started_at: str
    finished_at: str
    phase: str = "final"
    processed_messages: int = 0
    processed_audit_events: int = 0
    daily_events: int = 0
    candidates: int = 0
    promoted: int = 0
    failure_code: str = ""
    warnings: tuple[str, ...] = ()
    cursor_before: dict[str, object] = field(default_factory=dict)
    cursor_after: dict[str, object] = field(default_factory=dict)
    recovery: dict[str, object] = field(default_factory=dict)
    # 失败诊断只保留**机器可判定的形状**：异常类名 + 供应商 HTTP 状态码（有则记）。
    # 供应商异常正文、prompt、记忆内容一律不落盘（沿用既有红线），但"只知道失败码"会让
    # CURATOR_MODEL_FAILED 这类通用码无法定位——本轮 real-machine 故障正是卡在这里。
    failure_diagnostic: dict[str, object] = field(default_factory=dict)
    schema_version: str = CURATOR_RUN_SCHEMA_VERSION
    record_id: str = ""

    # LLM: Host normalization assigns record_id and validates bounded metadata before any file
    # mutation, so malformed provider values cannot corrupt the audit ledger.
    # 函数用途: 返回稳定、可序列化的 v2 run audit 对象。
    def to_record(self) -> dict[str, object]:
        normalized = normalize_run_record(self)
        payload = asdict(normalized)
        payload["warnings"] = list(normalized.warnings)
        return payload

    # LLM: Retention/doctor read the same strict contract that writers produce; future unknown
    # keys are not accepted as hidden content channels.
    # 函数用途: 从 JSONL 行恢复并验证一条 run audit。
    @classmethod
    def from_record(cls, payload: dict[str, object]) -> CuratorRunRecord:
        if set(payload) != set(cls.__dataclass_fields__):
            raise ValueError("memory curator run audit fields do not match v2 schema")
        try:
            return normalize_run_record(cls(**payload))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid memory curator run audit") from exc


# LLM: The repository is the sole append path for run audit and applies exact record-id
# idempotency under the existing per-path cross-process lock.
# 类用途: 管理 memory/curator/runs/YYYY-MM-DD.jsonl。
class CuratorRunLog:
    # LLM: 构造器只绑定当前 owner 的 runs 根，不读取或重放历史运行。
    # 函数用途: 初始化 Curator 无正文运行审计仓库。
    def __init__(self, runs_dir: str | Path) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # LLM: Same record replay is a no-op; same record_id with different bytes is a hard
    # collision rather than an overwrite.
    # 函数用途: 原子追加一次 Curator 运行审计。
    def append(self, record: CuratorRunRecord) -> CuratorRunRecord:
        normalized = normalize_run_record(record)
        path = run_log_path(self.runs_dir, normalized.finished_at)
        with locked_json_path(path):
            existing = load_run_records_unlocked(path)
            rows = merge_run_record(existing, normalized)
            write_text_file_atomic_unlocked(path, run_records_text(rows))
        return normalized

    # LLM: Health and tests can inspect structured audit without parsing files independently.
    # 函数用途: 严格列出所有日分片中的 run audit。
    def list(self) -> list[CuratorRunRecord]:
        records: list[CuratorRunRecord] = []
        for path in sorted(self.runs_dir.glob("*.jsonl")):
            with locked_json_path(path):
                records.extend(load_run_records_unlocked(path))
        return records


# LLM: Date sharding is storage layout only and never determines truth, retry order, or lease
# ownership.
# 函数用途: 根据带时区 ISO 完成时间选择 run audit 日分片。
def run_log_path(runs_dir: str | Path, finished_at: str) -> Path:
    value = normalize_iso_time(finished_at, default=utc_now_iso(), allow_empty=False)
    return Path(runs_dir) / f"{value[:10]}.jsonl"


# LLM: Run audit normalization bounds all free text and counters while retaining exact cursor
# maps for post-run evidence.
# 函数用途: 校验并补齐 run audit 的稳定 record_id。
def normalize_run_record(record: CuratorRunRecord) -> CuratorRunRecord:
    if not isinstance(record, CuratorRunRecord):
        raise TypeError("run audit must use CuratorRunRecord")
    if record.schema_version != CURATOR_RUN_SCHEMA_VERSION:
        raise ValueError("unsupported memory curator run audit schema")
    status = str(record.status or "").strip().lower()
    phase = str(record.phase or "").strip().lower()
    if status not in _RUN_AUDIT_STATUSES or phase not in _RUN_AUDIT_PHASES:
        raise ValueError("invalid memory curator run audit lifecycle")
    identifiers = [record.run_id, record.lease_id, record.reason, record.provider, record.model]
    if not str(record.run_id or "").strip() or any(len(str(item or "")) > 300 for item in identifiers):
        raise ValueError("invalid memory curator run audit identity")
    if str(record.reason or "").strip() not in CURATOR_TRIGGER_REASONS:
        raise ValueError("invalid memory curator run audit reason")
    counts = (
        record.processed_messages,
        record.processed_audit_events,
        record.daily_events,
        record.candidates,
        record.promoted,
    )
    if any(int(value) < 0 for value in counts):
        raise ValueError("memory curator run audit counts cannot be negative")
    started_at = normalize_iso_time(record.started_at, default=utc_now_iso(), allow_empty=False)
    finished_at = normalize_iso_time(record.finished_at, default=utc_now_iso(), allow_empty=False)
    expected_record_id = _stable_record_id(
        record.run_id,
        phase,
        status,
        finished_at,
    )
    record_id = str(record.record_id or "").strip() or expected_record_id
    if record_id != expected_record_id:
        raise ValueError("memory curator run record_id mismatch")
    return CuratorRunRecord(
        **{
            **asdict(record),
            "status": status,
            "phase": phase,
            "started_at": started_at,
            "finished_at": finished_at,
            "warnings": tuple(_bounded_warnings(record.warnings)),
            "cursor_before": _normalized_cursor(record.cursor_before),
            "cursor_after": _normalized_cursor(record.cursor_after),
            "recovery": _normalized_recovery(record.recovery),
            "record_id": record_id,
        }
    )


# LLM: Strict loading fails closed on any malformed row so a rewrite cannot silently erase
# historical run evidence.
# 函数用途: 在调用方持有文件锁时读取一个 run audit 分片。
def load_run_records_unlocked(path: Path) -> list[CuratorRunRecord]:
    report = read_jsonl_objects_report(path, context="memory_curator.runs")
    if report.load_errors:
        raise RuntimeError("memory curator run audit is unreadable")
    try:
        records = [CuratorRunRecord.from_record(item) for item in report.records]
    except ValueError as exc:
        raise RuntimeError("memory curator run audit failed schema validation") from exc
    ids = [item.record_id for item in records]
    if len(ids) != len(set(ids)):
        raise RuntimeError("memory curator run audit contains duplicate record_id")
    return records


# LLM: This pure merge is reused by the multi-file Curator transaction and standalone failure
# audit, preserving one collision rule.
# 函数用途: 幂等合并一条 run audit 到已验证记录列表。
def merge_run_record(
    existing: list[CuratorRunRecord],
    record: CuratorRunRecord,
) -> list[CuratorRunRecord]:
    normalized = normalize_run_record(record)
    for prior in existing:
        if prior.record_id != normalized.record_id:
            continue
        if prior != normalized:
            raise RuntimeError("memory curator run record_id collision")
        return list(existing)
    return [*existing, normalized]


# LLM: Serialization contains exactly one JSON object per line and no prompt/model body.
# 函数用途: 将已验证 run audit 记录序列化为日分片文本。
def run_records_text(records: list[CuratorRunRecord]) -> str:
    return "".join(
        json.dumps(item.to_record(), ensure_ascii=False, sort_keys=True) + "\n"
        for item in records
    )


# LLM: Stable identity includes phase/status/time so recovery and final events for one run remain
# separately addressable while exact replay stays idempotent.
# 函数用途: 生成 run audit record_id。
def _stable_record_id(run_id: str, phase: str, status: str, finished_at: str) -> str:
    material = "\x1f".join((str(run_id), phase, status, finished_at))
    return "memory-curator-audit-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


# LLM: Warnings are diagnostic codes/short labels only, never provider exception text or user
# content.
# 函数用途: 限制 run audit warnings 的数量和长度。
def _bounded_warnings(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("memory curator run warnings must be an array")
    result = [str(value).strip() for value in values if str(value).strip()]
    if len(result) > 32 or any(len(value) > 300 for value in result):
        raise ValueError("memory curator run warnings exceed bounds")
    return list(dict.fromkeys(result))


# LLM: Cursor audit accepts only the no-content cursor shape; arbitrary dicts cannot become a
# covert copy of provider output.
# 函数用途: 校验 run audit 中的前后游标投影。
def _normalized_cursor(value: object) -> dict[str, object]:
    if value == {}:
        return {}
    if not isinstance(value, dict) or set(value) != {
        "per_thread_cursors",
        "last_audit_event_id",
    }:
        raise ValueError("memory curator run cursor shape is invalid")
    cursors = value.get("per_thread_cursors")
    if not isinstance(cursors, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in cursors.items()
    ):
        raise ValueError("memory curator run per-thread cursor is invalid")
    audit_id = value.get("last_audit_event_id")
    if not isinstance(audit_id, str):
        raise ValueError("memory curator run audit cursor is invalid")
    return {"per_thread_cursors": dict(cursors), "last_audit_event_id": audit_id}


# LLM: Recovery metadata is a closed host-generated union containing identities/timestamps only;
# it cannot carry model/user content.
# 函数用途: 校验 run audit 的崩溃接管或事务回滚来源。
def _normalized_recovery(value: object) -> dict[str, object]:
    if value == {}:
        return {}
    if not isinstance(value, dict):
        raise ValueError("memory curator run recovery must be an object")
    kind = str(value.get("kind") or "")
    allowed = {
        "expired_lease": {
            "kind",
            "previous_run_id",
            "previous_lease_id",
            "previous_expires_at",
            "recovered_at",
        },
        "transaction_rollback": {"kind", "prepared_at"},
        "manual_from_quarantine": {
            "kind",
            "sentinel_sha256",
            "quarantine_path",
            "error_class",
            "restored_from_run_id",
            "restored_at",
        },
    }
    if kind not in allowed or set(value) != allowed[kind]:
        raise ValueError("memory curator run recovery shape is invalid")
    result = {key: str(item or "") for key, item in value.items()}
    if any(len(item) > 300 for item in result.values()):
        raise ValueError("memory curator run recovery value is too long")
    return result


__all__ = [
    "CuratorRunLog",
    "CuratorRunRecord",
    "load_run_records_unlocked",
    "merge_run_record",
    "normalize_run_record",
    "run_log_path",
    "run_records_text",
]
