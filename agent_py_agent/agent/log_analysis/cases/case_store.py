# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Finding-to-case persistence on top of the local log-analysis store."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import CaseRecord, EvidenceRef, Finding, QueryPlan, utc_now_iso
from ..storage.local_store import LocalLogStore
from .case_helpers import (
    _bucket_from_dedup_key,
    _case_type_for_findings,
    _clamp_score,
    _ensure_case,
    _ensure_finding,
    _entity_values,
    _merge_entities,
    _merge_evidence_refs,
    _parse_time,
    _primary_account,
    _primary_attacker,
    _primary_victim,
    _stable_case_id,
    _time_bucket,
    _title_for_finding,
    _unique,
    _unique_values,
)

Case = CaseRecord


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 CaseStore 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 封装 CaseStore 的持久化入口，把路径、读写和查询操作集中到同一对象。
class CaseStore:
    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 __init__ 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 初始化实例依赖、路径或缓存状态，为同一对象的后续方法提供共享上下文。
    def __init__(
        self,
        root: str | Path | LocalLogStore,
        *,
        min_case_confidence: float = 0.6,
        merge_window_minutes: int = 15,
        search_store: Any | None = None,
    ) -> None:
        self.store = root if isinstance(root, LocalLogStore) else LocalLogStore(root)
        self.min_case_confidence = min_case_confidence
        self.merge_window_minutes = merge_window_minutes
        self.search_store = search_store

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 root 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 root 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
    @property
    def root(self) -> Path:
        return self.store.root

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 record_finding 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 record finding 相关记录，集中处理目标路径、格式化和状态更新。
    def record_finding(self, finding: Finding | Mapping[str, Any]) -> CaseRecord | None:
        normalized = _ensure_finding(finding)
        self.store.upsert_finding(normalized)
        if (normalized.confidence if normalized.confidence is not None else normalized.risk_score) < self.min_case_confidence:
            return None
        return self.upsert_case_for_finding(normalized)

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 record_findings 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 record findings 相关记录，集中处理目标路径、格式化和状态更新。
    def record_findings(self, findings: Sequence[Finding | Mapping[str, Any]]) -> list[CaseRecord]:
        cases: list[CaseRecord] = []
        seen: set[str] = set()
        for finding in findings:
            case = self.record_finding(finding)
            if case is None or case.case_id in seen:
                continue
            seen.add(case.case_id)
            cases.append(case)
        return cases

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 upsert_case_for_finding 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 upsert case for finding 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
    def upsert_case_for_finding(self, finding: Finding | Mapping[str, Any]) -> CaseRecord:
        normalized = _ensure_finding(finding)
        dedup_key = dedup_key_for_finding(normalized, self.merge_window_minutes)
        case = self.get_case_by_dedup_key(dedup_key) or self._find_merge_candidate(normalized, dedup_key)
        if case is None:
            case = CaseRecord(
                case_id=_stable_case_id(dedup_key),
                title=_title_for_finding(normalized),
                priority=priority_for_score(normalized.risk_score),
                risk_score=normalized.risk_score,
                dedup_key=dedup_key,
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
                case_type=_case_type_for_findings([normalized]),
            )
        self._merge_finding(case, normalized)
        self.store.upsert_case(case)
        self._index_case(case)
        return case

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 save_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 save case 相关记录，集中处理目标路径、格式化和状态更新。
    def save_case(self, case: CaseRecord | Mapping[str, Any]) -> None:
        self.store.upsert_case(_ensure_case(case))

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 get_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 get case 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def get_case(self, case_id: str) -> CaseRecord | None:
        payload = self.store.get_case(case_id)
        return CaseRecord.from_dict(payload) if payload else None

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 get_case_by_dedup_key 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 get case by dedup key 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def get_case_by_dedup_key(self, dedup_key: str) -> CaseRecord | None:
        for case in self.list_cases():
            if case.dedup_key == dedup_key:
                return case
        return None

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 list_cases 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 list cases 的候选结果，并按参数完成筛选、排序或数量限制。
    def list_cases(self) -> list[CaseRecord]:
        cases = [CaseRecord.from_dict(item) for item in self.store.list_cases()]
        return sorted(cases, key=lambda item: (item.priority, -item.risk_score, item.updated_at))

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 load_findings 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 读取 load findings 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
    def load_findings(self) -> list[Finding]:
        return [Finding.from_dict(item) for item in self.store.list_findings()]

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _find_merge_candidate 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 收集或查询 find merge candidate 的候选结果，并按参数完成筛选、排序或数量限制。
    def _find_merge_candidate(self, finding: Finding, dedup_key: str) -> CaseRecord | None:
        victim = _primary_victim(finding)
        attacker = _primary_attacker(finding)
        bucket = _bucket_from_dedup_key(dedup_key)
        if not victim or not bucket:
            return None
        for case in self.list_cases():
            if case.status in {"CLOSED", "SUPPRESSED"}:
                continue
            if _bucket_from_dedup_key(case.dedup_key) != bucket:
                continue
            case_victims = set(_entity_values(case.entities, "victim_ip", "dst_ip", "host", "asset_id"))
            if victim not in case_victims:
                continue
            case_attackers = set(_entity_values(case.entities, "attacker_ip"))
            if not attacker or not case_attackers or attacker in case_attackers:
                return case
        return None

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _merge_finding 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 提取、合并或规范化 merge finding 涉及的字段，让后续匹配和存储使用同一形态。
    def _merge_finding(self, case: CaseRecord, finding: Finding) -> None:
        case.risk_score = max(case.risk_score, finding.risk_score)
        case.priority = min_priority(case.priority, priority_for_score(case.risk_score))
        case.finding_refs = _unique([*case.finding_refs, finding.finding_id])
        case.evidence_refs = _merge_evidence_refs(case.evidence_refs, finding.evidence_refs)
        case.entities = _merge_entities(case.entities, finding.entities)
        case.facts = _unique([*case.facts, f"{finding.detector_id} finding observed"])
        if finding.hypothesis:
            case.inferences = _unique([*case.inferences, finding.hypothesis])
        case.gaps = _unique([*case.gaps, *finding.gaps])
        case.next_queries = _unique_values([*case.next_queries, *finding.next_queries])
        attributes = case.attributes if isinstance(case.attributes, dict) else {}
        summaries = list(attributes.get("finding_summaries", []))
        if not any(item.get("finding_id") == finding.finding_id for item in summaries if isinstance(item, dict)):
            summaries.append(finding.to_dict())
        case.attributes = {**attributes, "finding_summaries": summaries}
        case.case_type = _case_type_for_findings([Finding.from_dict(item) for item in summaries if isinstance(item, dict)])
        case.updated_at = utc_now_iso()

    # LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 _index_case 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 写入或登记 index case 相关记录，集中处理目标路径、格式化和状态更新。
    def _index_case(self, case: CaseRecord) -> None:
        if self.search_store is None or not hasattr(self.search_store, "upsert_record"):
            return
        self.search_store.upsert_record(
            source_type="log_analysis_case",
            source_id=case.case_id,
            title=case.title or case.case_id,
            content=case.to_json(),
            metadata={
                "priority": case.priority,
                "risk_score": case.risk_score,
                "dedup_key": case.dedup_key,
                "finding_count": len(case.finding_refs),
            },
        )


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 dedup_key_for_finding 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 dedup key for finding 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def dedup_key_for_finding(finding: Finding | Mapping[str, Any], window_minutes: int = 15) -> str:
    normalized = _ensure_finding(finding)
    attacker = _primary_attacker(normalized) or "unknown"
    victim = _primary_victim(normalized) or "unknown"
    bucket = _time_bucket(normalized.window[0] if normalized.window else "", window_minutes)
    if normalized.detector_id in {"vpn_new_geo_login", "bruteforce_then_success"}:
        account = _primary_account(normalized) or "unknown"
        return f"attack={attacker}|account={account}|victim={victim}|bucket={bucket}"
    return f"attack={attacker}|victim={victim}|bucket={bucket}"


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 priority_for_score 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 priority for score 的判定结果，避免把推测当作事实写入。
def priority_for_score(score: float) -> str:
    clean = _clamp_score(score)
    if clean >= 0.85:
        return "P0"
    if clean >= 0.7:
        return "P1"
    if clean >= 0.55:
        return "P2"
    return "P3"


# LLM: case 流程把 finding、evidence 和调度状态写入可追踪案例；修改 min_priority 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 min priority 在当前模块中的核心转换或协调步骤，衔接 case 流程把 finding、evidence 和调度状态写入可追踪案例。
def min_priority(left: str, right: str) -> str:
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return left if order.get(left, 99) <= order.get(right, 99) else right


__all__ = ["Case", "CaseStore", "dedup_key_for_finding", "min_priority", "priority_for_score"]
