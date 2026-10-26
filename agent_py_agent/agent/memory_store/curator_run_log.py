from __future__ import annotations

"""Memory Curator 无正文运行审计。"""

# LLM: initialize=False仅纯路径绑定零初始化I/O，不授append权；默认保原mkdir，原审计只记录生命周期/count/cursor，不收正文。
# LLM: Run audit records lifecycle/count/cursor facts only; prompts, responses, daily summaries,
# candidate bodies, and transaction backups never enter this ledger.
# 模块用途: 为成功、失败和崩溃回滚生成幂等的 memory/curator/runs 日分片记录。

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic_unlocked,
)
from .candidate_models import normalize_iso_time, utc_now_iso
from .curator_models import CURATOR_RUN_SCHEMA_VERSION, CURATOR_TRIGGER_REASONS

_RUN_AUDIT_STATUSES = frozenset({"succeeded", "failed", "recovered_rollback"})
# 一条运行记录最多保留的警告条数：运行账只放诊断码/短标签，条数有界。
RUN_WARNINGS_MAX_COUNT = 32
# 运行记录里每条警告的最大字符数；写入方须先截短，超出会让整次记账失败。
RUN_WARNING_MAX_CHARS = 300
_RUN_AUDIT_PHASES = frozenset({"final", "recovery"})
# 版本元数据复用严格v2的有界宿主warnings槽位，cursor本体仍只有原两项且线程值全是字符串。
_CURSOR_CHECKPOINT = "cursor_checkpoint_v1="
_CURSOR_DELTA = "cursor_delta_v1="


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


# LLM: 仓库仍是原run audit唯一追加路径，record-id幂等及原跨进程锁不变；零初始化构造不创建目录或授予执行。
# 类用途: 绑定并管理原Curator运行审计，冷metadata只绑定路径。
class CuratorRunLog:
    # LLM: 默认保原runs目录初始化；initialize=False纯路径绑定零I/O，不改变后续append原账/锁/调用者许可，不读或重放历史。
    # 函数用途: 绑定当前owner无正文运行审计，冷metadata可显式跳过目录创建。
    def __init__(self, runs_dir: str | Path, *, initialize: bool = True) -> None:
        self.runs_dir = Path(runs_dir)
        if initialize:
            self.runs_dir.mkdir(parents=True, exist_ok=True)

    # LLM: 普通追加自行取锁；恢复方持同一审计分片锁时共用锁内追加，不在 state 锁内另取文件锁。
    # 函数用途: 追加无正文审计；大型游标先生成版本化检查点或增量，再沿原锁原子写入。
    def append(self, record: CuratorRunRecord, *, relevant_thread_ids: tuple[str, ...] = ()) -> CuratorRunRecord:
        normalized = self.cursor_record(record, relevant_thread_ids=relevant_thread_ids)
        path = run_log_path(self.runs_dir, normalized.finished_at)
        with locked_json_path(path):
            return self._append_unlocked(normalized)

    # LLM: 首个大型游标记录在原分片内自带完整检查点，后续只投影批内/变化线程；前序ID及完整摘要绑定整链。
    # 函数用途: 为事务和失败追加共用瘦身，按展开全景核验重放，缩回小字典也保持既有版本链。
    def cursor_record(self, record: CuratorRunRecord, *, relevant_thread_ids: tuple[str, ...] = ()) -> CuratorRunRecord:
        submitted = record = normalize_run_record(record)
        warnings = tuple(value for value in record.warnings if not value.startswith((_CURSOR_CHECKPOINT, _CURSOR_DELTA)))
        record = replace(record, warnings=warnings)
        if not _cursor_record(record):
            return record
        before, after = record.cursor_before, record.cursor_after or record.cursor_before
        before_threads, after_threads = before["per_thread_cursors"], after["per_thread_cursors"]
        path = run_log_path(self.runs_dir, record.finished_at)
        with locked_json_path(path):
            rows = load_run_records_unlocked(path)
        existing = next((row for row in rows if row.record_id == record.record_id), None)
        if existing is not None:
            full = next(row for row in expand_cursor_records(rows, allow_anchor=True) if row.record_id == record.record_id)
            clean = replace(full, warnings=tuple(value for value in full.warnings
                                                if not value.startswith((_CURSOR_CHECKPOINT, _CURSOR_DELTA))))
            if submitted != existing and record != clean:
                raise RuntimeError("memory curator run record_id collision")
            return existing
        prior = [row for row in rows if _cursor_record(row)]
        previous = prior[-1] if prior else self._previous_cursor_record()
        linked = any(_chain_marker(row) for row in prior)
        small = max(len(before_threads), len(after_threads)) <= 32
        if small and not linked and not (previous is not None and _chain_marker(previous)):
            return record
        checkpoint = not linked or small or bool(set(before_threads) - set(after_threads))
        previous_id = previous.record_id if previous is not None else ""
        changed = {tid for tid in set(before_threads) | set(after_threads) if before_threads.get(tid) != after_threads.get(tid)}
        relevant = set(relevant_thread_ids) | changed
        prefix = _CURSOR_CHECKPOINT if checkpoint else _CURSOR_DELTA
        marker = prefix + ":".join((previous_id, _cursor_digest(before), _cursor_digest(after)))
        return replace(record,
            cursor_before=before if checkpoint else project_cursor_payload(before_threads, before["last_audit_event_id"], relevant),
            cursor_after=(record.cursor_after if checkpoint else project_cursor_payload(after_threads, after["last_audit_event_id"], relevant))
                         if record.cursor_after else {},
            warnings=(marker, *warnings[:RUN_WARNINGS_MAX_COUNT - 1]))

    # LLM: 跨日只取最近带游标的原记录，完整检查点仍写回本日分片；不从state或自然语言猜前序链接。
    # 函数用途: 给新分片绑定上一个已存在的游标记录ID。
    def _previous_cursor_record(self) -> CuratorRunRecord | None:
        for path in sorted(self.runs_dir.glob("*.jsonl"), reverse=True):
            with locked_json_path(path):
                rows = load_run_records_unlocked(path)
            found = next((row for row in reversed(rows) if _cursor_record(row)), None)
            if found is not None:
                return found
        return None

    # LLM: 只能在 finished_at 对应分片锁已持有时调用；同 ID 碰撞与严格历史读取规则不变。
    # 函数用途: 在多文件恢复临界区内落恢复审计，避免再次取 flock 及锁序倒置。
    def _append_unlocked(self, record: CuratorRunRecord) -> CuratorRunRecord:
        normalized = normalize_run_record(record)
        path = run_log_path(self.runs_dir, normalized.finished_at)
        existing = load_run_records_unlocked(path)
        rows = merge_run_record(existing, normalized)
        write_text_file_atomic_unlocked(path, run_records_text(rows))
        return normalized

    # LLM: 只读；从最新的日分片往前读，只收已结束（succeeded/failed）的记录，按完成时间新到旧，凑够 count 条即停。
    #   供重放熔断判断“同一游标是否连续失败”，不扫全部历史。
    # 函数用途: 从本日检查点展开完整游标后，返回最近count条运行，供原分组和熔断读口使用。
    def recent_finished(self, count: int) -> list[CuratorRunRecord]:
        found: list[CuratorRunRecord] = []
        for path in sorted(self.runs_dir.glob("*.jsonl"), reverse=True):
            with locked_json_path(path):
                rows = load_run_records_unlocked(path)
            expanded = expand_cursor_records(rows, allow_anchor=True)
            found.extend(row for row in expanded if row.status in {"succeeded", "failed"})
            if len(found) >= count:
                break
        return sorted(found, key=lambda row: row.finished_at, reverse=True)[:count]

    # LLM: Health and tests can inspect structured audit without parsing files independently.
    # 函数用途: 严格核验跨分片检查点与增量链，返回完整游标视图；断链或损坏一律拒绝。
    def list(self) -> list[CuratorRunRecord]:
        records: list[CuratorRunRecord] = []
        for path in sorted(self.runs_dir.glob("*.jsonl")):
            with locked_json_path(path):
                records.extend(load_run_records_unlocked(path))
        return expand_cursor_records(records)


# LLM: 小型字典保持原样；大型投影只含本批与实际变化线程，键值仍严格为str，不在cursor塞隐藏元数据。
# 函数用途: 生成严格兼容原游标形状的全量或增量投影。
def project_cursor_payload(cursors: dict[str, str], audit: str, relevant: set[str] | None = None) -> dict[str, object]:
    projected = dict(cursors) if relevant is None or len(cursors) <= 32 else {
        tid: value for tid, value in sorted(cursors.items()) if tid in relevant}
    return {"per_thread_cursors": projected, "last_audit_event_id": str(audit or "")}


# LLM: 仅最终运行且带before游标的记录进入增量链；隔离与人工恢复审计不伪装成新的处理批次。
# 函数用途: 识别需要维护连续游标视图的原运行记录。
def _cursor_record(record: CuratorRunRecord) -> bool:
    return record.phase == "final" and bool(record.cursor_before)


# LLM: 摘要基于严格原两字段的确定性JSON；不接受任意输出对象作为游标权威。
# 函数用途: 为完整游标视图生成跨增量链的校验指纹。
def _cursor_digest(cursor: dict) -> str:
    material = json.dumps(_normalized_cursor(cursor), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# LLM: 宿主版本标记只允许一种且三个字段格式严格；缺链、坏摘要与重复标记不能被当作旧全量记录。
# 函数用途: 解码版本化检查点或增量标记，未知字段仍不进入cursor本体。
def _chain_marker(record: CuratorRunRecord):
    markers = [value for value in record.warnings if value.startswith((_CURSOR_CHECKPOINT, _CURSOR_DELTA))]
    if not markers:
        return None
    if len(markers) != 1:
        raise RuntimeError("curator cursor chain marker is ambiguous")
    checkpoint = markers[0].startswith(_CURSOR_CHECKPOINT)
    prefix = _CURSOR_CHECKPOINT if checkpoint else _CURSOR_DELTA
    parts = markers[0][len(prefix):].split(":")
    if (len(parts) != 3 or any(len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value) for value in parts[1:])
            or (parts[0] and not parts[0].startswith("memory-curator-audit-"))):
        raise RuntimeError("curator cursor chain marker is invalid")
    return checkpoint, *parts


# LLM: 全历史读取验证每条前序ID和完整摘要；近期读取只允许从完整分片检查点起步，不可从任意delta起步。
# 函数用途: 按物理追加顺序展开原游标全景，供恢复、分组熔断和doctor共用；断链立即拒绝。
def expand_cursor_records(records: list[CuratorRunRecord], *, allow_anchor: bool = False) -> list[CuratorRunRecord]:
    result, previous, full, chained = [], "", None, False
    for record in records:
        if not _cursor_record(record):
            result.append(record)
            continue
        marker = _chain_marker(record)
        if marker is None:
            if chained:
                raise RuntimeError("curator cursor delta chain lost its version marker")
            full = record.cursor_after or record.cursor_before
            result.append(record)
        else:
            checkpoint, prior_id, before_hash, after_hash = marker
            if prior_id != previous and not (allow_anchor and full is None and checkpoint):
                raise RuntimeError("curator cursor delta chain lost a prior record")
            if checkpoint:
                before = record.cursor_before
                if full is not None and before != full:
                    raise RuntimeError("curator cursor checkpoint diverged")
                after = record.cursor_after or before
            else:
                if not chained or full is None:
                    raise RuntimeError("curator cursor delta chain lacks a checkpoint")
                before = full
                projected = record.cursor_before
                if (projected["last_audit_event_id"] != before["last_audit_event_id"]
                        or any(before["per_thread_cursors"].get(tid) != value for tid, value in projected["per_thread_cursors"].items())):
                    raise RuntimeError("curator cursor delta before-image mismatches")
                delta = record.cursor_after or projected
                cursors = {**before["per_thread_cursors"], **delta["per_thread_cursors"]}
                after = project_cursor_payload(cursors, delta["last_audit_event_id"])
            if _cursor_digest(before) != before_hash or _cursor_digest(after) != after_hash:
                raise RuntimeError("curator cursor delta digest mismatches")
            result.append(replace(record, cursor_before=dict(before), cursor_after=dict(after) if record.cursor_after else {}))
            full, chained = after, True
        previous = record.record_id
    return result


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
    if len(result) > RUN_WARNINGS_MAX_COUNT or any(len(value) > RUN_WARNING_MAX_CHARS for value in result):
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
    # LLM: stale_lease_reclaimed 是 E11d 提前接管事实（mc4-1801）：与 expired_lease 同格式对齐，
    #   只含结构化标识与时间；字段全集严格匹配，不认的 kind 或多余字段一律拒绝，
    #   不能把接管事实丢成空 recovery 绕过校验。
    allowed = {
        "expired_lease": {
            "kind",
            "previous_run_id",
            "previous_lease_id",
            "previous_expires_at",
            "recovered_at",
        },
        "stale_lease_reclaimed": {
            "kind",
            "previous_run_id",
            "previous_lease_id",
            "previous_pid",
            "previous_expires_at",
            "reclaimed_at",
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
