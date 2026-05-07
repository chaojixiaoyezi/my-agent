# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""JSONL-backed local store for the first log-analysis milestone."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...io import append_jsonl
from .base import (
    CASE_ID_FIELDS,
    EVENT_ID_FIELDS,
    EVIDENCE_ID_FIELDS,
    FINDING_ID_FIELDS,
    Case,
    EvidenceRef,
    Finding,
    JsonlReadAudit,
    NormalizedEvent,
    QueryRecord,
    canonical_json,
    default_log_analysis_root,
    model_to_dict,
    record_identity,
    utc_now,
)


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _AuditSampleInput 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _AuditSampleInput 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _AuditSampleInput:
    audit: JsonlReadAudit
    line_no: int
    reason: str
    line: str
    detail: str = ""
    max_samples: int = 5


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 LocalLogStore 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 LocalLogStore 的持久化入口，把路径、读写和查询操作集中到同一对象。
class LocalLogStore:
    """Small append-friendly store whose public contract can move to DuckDB later."""

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else default_log_analysis_root()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "evidence").mkdir(parents=True, exist_ok=True)
        self._last_read_audits: dict[str, dict[str, Any]] = {}

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 events_path 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 events path 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 findings_path 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 findings path 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    @property
    def findings_path(self) -> Path:
        return self.root / "findings.jsonl"

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 cases_path 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 cases path 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    @property
    def cases_path(self) -> Path:
        return self.root / "cases.jsonl"

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 evidence_refs_path 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 计算 evidence refs path 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
    @property
    def evidence_refs_path(self) -> Path:
        return self.root / "evidence_refs.jsonl"

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 queries_path 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 queries path 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    @property
    def queries_path(self) -> Path:
        return self.root / "queries.jsonl"

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 corrupt_lines_path 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 corrupt lines path 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    @property
    def corrupt_lines_path(self) -> Path:
        return self.root / "corrupt_lines.jsonl"

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 last_read_audit 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 last read audit 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def last_read_audit(self, path: str | Path | None = None) -> dict[str, Any]:
        if path is None:
            return dict(self._last_read_audits)
        return dict(self._last_read_audits.get(str(Path(path)), {}))

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_event 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert event 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_event(self, event: NormalizedEvent | dict[str, Any]) -> bool:
        return self._upsert_one(self.events_path, event, EVENT_ID_FIELDS)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_events 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert events 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_events(self, events: Iterable[NormalizedEvent | dict[str, Any]]) -> int:
        return sum(1 for event in events if self.upsert_event(event))

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_events 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list events 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_events(self) -> list[dict[str, Any]]:
        return list(self._read_unique(self.events_path, EVENT_ID_FIELDS).values())

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 get_event 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 get event 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def get_event(self, event_id: str) -> dict[str, Any] | None:
        return self._read_unique(self.events_path, EVENT_ID_FIELDS).get(event_id)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_finding 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert finding 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_finding(self, finding: Finding | dict[str, Any]) -> bool:
        return self._upsert_one(self.findings_path, finding, FINDING_ID_FIELDS)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_findings 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert findings 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_findings(self, findings: Iterable[Finding | dict[str, Any]]) -> int:
        return sum(1 for finding in findings if self.upsert_finding(finding))

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_findings 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list findings 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_findings(self) -> list[dict[str, Any]]:
        return list(self._read_unique(self.findings_path, FINDING_ID_FIELDS).values())

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 get_finding 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 get finding 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def get_finding(self, finding_id: str) -> dict[str, Any] | None:
        return self._read_unique(self.findings_path, FINDING_ID_FIELDS).get(finding_id)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert case 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_case(self, case: Case | dict[str, Any]) -> bool:
        return self._upsert_one(self.cases_path, case, CASE_ID_FIELDS)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_cases 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert cases 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def upsert_cases(self, cases: Iterable[Case | dict[str, Any]]) -> int:
        return sum(1 for case in cases if self.upsert_case(case))

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_cases 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list cases 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_cases(self) -> list[dict[str, Any]]:
        return list(self._read_unique(self.cases_path, CASE_ID_FIELDS).values())

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 get_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 get case 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def get_case(self, case_id: str) -> dict[str, Any] | None:
        return self._read_unique(self.cases_path, CASE_ID_FIELDS).get(case_id)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_evidence_ref 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 计算 upsert evidence ref 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
    def upsert_evidence_ref(self, ref: EvidenceRef | dict[str, Any]) -> bool:
        return self._upsert_one(self.evidence_refs_path, ref, EVIDENCE_ID_FIELDS)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 upsert_evidence_refs 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 计算 upsert evidence refs 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
    def upsert_evidence_refs(self, refs: Iterable[EvidenceRef | dict[str, Any]]) -> int:
        return sum(1 for ref in refs if self.upsert_evidence_ref(ref))

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_evidence_refs 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list evidence refs 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_evidence_refs(self) -> list[dict[str, Any]]:
        return list(self._read_unique(self.evidence_refs_path, EVIDENCE_ID_FIELDS).values())

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 get_evidence_ref 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 get evidence ref 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def get_evidence_ref(self, evidence_id: str) -> dict[str, Any] | None:
        return self._read_unique(self.evidence_refs_path, EVIDENCE_ID_FIELDS).get(evidence_id)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 save_query_record 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 save query record 相关记录，集中处理目标路径、格式化和状态更新。
    def save_query_record(self, record: QueryRecord | dict[str, Any]) -> None:
        append_jsonl(self.queries_path, model_to_dict(record), sort_keys=True)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 list_query_records 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list query records 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_query_records(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.queries_path)

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _upsert_one 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert one 在当前模块中的核心转换或协调步骤，衔接 日志分析存储层读写本地事件、finding、case 和 evidence 投影。
    def _upsert_one(self, path: Path, record: Any, id_fields: tuple[str, ...]) -> bool:
        payload = model_to_dict(record)
        record_id = record_identity(payload, id_fields)
        existing = self._read_unique(path, id_fields)
        current = existing.get(record_id)
        if current is not None and canonical_json(current) == canonical_json(payload):
            return False
        append_jsonl(path, payload, sort_keys=True)
        return True

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _read_unique 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 read unique 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def _read_unique(self, path: Path, id_fields: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for payload in self._read_jsonl(path):
            records[record_identity(payload, id_fields)] = payload
        return records

    # LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _read_jsonl 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 read jsonl 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        audit = JsonlReadAudit(path=str(path), read_at=utc_now())
        if not path.exists():
            self._last_read_audits[str(path)] = model_to_dict(audit)
            return []
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            audit.total_lines += 1
            if not line.strip():
                audit.blank_lines += 1
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                audit.corrupt_lines += 1
                audit.skipped_lines += 1
                _add_audit_sample(_AuditSampleInput(audit, line_no, "invalid_json", line, str(exc)))
                continue
            if isinstance(payload, dict):
                records.append(payload)
                audit.valid_records += 1
                continue
            audit.non_object_lines += 1
            audit.skipped_lines += 1
            _add_audit_sample(_AuditSampleInput(audit, line_no, "non_object_json", line))
        audit_payload = model_to_dict(audit)
        self._last_read_audits[str(path)] = audit_payload
        if path != self.corrupt_lines_path and (audit.corrupt_lines or audit.non_object_lines):
            append_jsonl(self.corrupt_lines_path, audit_payload, sort_keys=True)
        return records


# LLM: 日志分析存储层读写本地事件、finding、case 和 evidence 投影；修改 _add_audit_sample 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 add audit sample 相关记录，集中处理目标路径、格式化和状态更新。
def _add_audit_sample(data: _AuditSampleInput) -> None:
    if len(data.audit.samples) >= data.max_samples:
        return
    sample: dict[str, Any] = {
        "line_no": data.line_no,
        "reason": data.reason,
        "preview": data.line[:200],
    }
    if data.detail:
        sample["detail"] = data.detail
    data.audit.samples.append(sample)
