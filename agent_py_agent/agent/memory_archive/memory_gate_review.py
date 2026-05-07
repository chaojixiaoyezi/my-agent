# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""explicit review write-back for run-local memory gate candidates.

Human version:
This module records reviewer decisions in `memory_gate/decisions.jsonl`. An
approval here never writes main memory or creates a formal skill; it only marks
the candidate as eligible for a later explicit export workflow.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .memory_gate import MemoryGateResult, memory_gate_paths


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 MemoryGateReviewRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryGateReviewRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class MemoryGateReviewRequest:
    """Review request fields bundled to avoid wide helper signatures."""

    candidate_id: str
    decision: str
    reviewer: str = "parent"
    note: str = ""
    now: float | None = None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 MemoryGateReviewResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryGateReviewResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class MemoryGateReviewResult:
    """Review write-back result for one gated candidate."""

    candidate: dict[str, object]
    decisions_jsonl: Path
    skill_spark_gate_json: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _ReviewFieldsParams 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 _ReviewFieldsParams 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class _ReviewFieldsParams:
    candidate: dict[str, object]
    decision: str
    request: MemoryGateReviewRequest
    reviewed_at: str


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 record_memory_gate_review 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 record memory gate review 相关记录，集中处理目标路径、格式化和状态更新。
def record_memory_gate_review(
    agent_run_workspace_root: Path,
    request: MemoryGateReviewRequest,
) -> MemoryGateReviewResult:
    """Record an explicit review decision without exporting memory or skills."""

    # LLM: approval here is only a gate decision; export commands must opt in later.
    paths = memory_gate_paths(agent_run_workspace_root)
    candidates = _read_jsonl(paths.candidates_jsonl)
    updated, reviewed = _apply_review_decision(candidates, request)
    _write_jsonl(paths.candidates_jsonl, updated)
    _write_jsonl(paths.review_queue_jsonl, _review_queue_records(updated))
    _append_jsonl(paths.decisions_jsonl, _decision_record(reviewed))
    _write_json(paths.skill_spark_gate_json, _review_gate_summary(paths, updated, reviewed))
    _merge_checkpoint_from_review(agent_run_workspace_root / "checkpoint.json", paths, updated)
    return MemoryGateReviewResult(reviewed, paths.decisions_jsonl, paths.skill_spark_gate_json)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _apply_review_decision 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 apply review decision 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _apply_review_decision(
    candidates: list[dict[str, object]],
    request: MemoryGateReviewRequest,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    normalized = _normalize_decision(request.decision)
    reviewed_at = _utc_iso(request.now or datetime.now(timezone.utc).timestamp())
    updated: list[dict[str, object]] = []
    reviewed: dict[str, object] | None = None
    for candidate in candidates:
        item = dict(candidate)
        if str(item.get("candidate_id") or "") == request.candidate_id:
            _apply_review_fields(_ReviewFieldsParams(item, normalized, request, reviewed_at))
            reviewed = item
        updated.append(item)
    if reviewed is None:
        raise FileNotFoundError(request.candidate_id)
    return updated, reviewed


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _apply_review_fields 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 apply review fields 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _apply_review_fields(params: _ReviewFieldsParams) -> None:
    # LLM: review field mutation is local to the run workspace and never exports automatically.
    status, promotion_status, requires_more_review = _decision_status(params.decision)
    candidate = params.candidate
    candidate["review_decision"] = params.decision
    candidate["review_status"] = status
    candidate["promotion_status"] = promotion_status
    candidate["review_required"] = requires_more_review
    candidate["gate_status"] = "needs_review" if requires_more_review else "reviewed"
    candidate["reviewer"] = params.request.reviewer or "parent"
    candidate["review_note"] = params.request.note
    candidate["reviewed_at"] = params.reviewed_at


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _decision_status 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 decision status 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _decision_status(decision: str) -> tuple[str, str, bool]:
    if decision == "approve_memory":
        return "approved", "approved_for_memory_export", False
    if decision == "approve_skill":
        return "approved", "approved_for_skill_export", False
    if decision == "reject":
        return "rejected", "rejected", False
    return "needs_evidence", "not_promoted", True


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _normalize_decision 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 normalize decision 涉及的字段，让后续匹配和存储使用同一形态。
def _normalize_decision(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"approve_memory", "approve_skill", "reject", "needs_evidence"}:
        return normalized
    raise ValueError(f"unsupported memory gate decision: {value}")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _decision_record 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 decision record 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _decision_record(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "version": 1,
        "candidate_id": candidate.get("candidate_id", ""),
        "candidate_type": candidate.get("candidate_type", ""),
        "run_id": candidate.get("run_id", ""),
        "task_id": candidate.get("task_id", ""),
        "review_decision": candidate.get("review_decision", ""),
        "review_status": candidate.get("review_status", ""),
        "promotion_status": candidate.get("promotion_status", ""),
        "reviewer": candidate.get("reviewer", ""),
        "review_note": candidate.get("review_note", ""),
        "reviewed_at": candidate.get("reviewed_at", ""),
        "auto_promote": False,
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _review_gate_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 review gate summary 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _review_gate_summary(
    paths: MemoryGateResult,
    candidates: list[dict[str, object]],
    reviewed: dict[str, object],
) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": reviewed.get("task_id", ""),
        "run_id": reviewed.get("run_id", ""),
        "candidate_count": len(candidates),
        "review_required_count": sum(1 for item in candidates if item.get("review_required")),
        "approved_count": sum(1 for item in candidates if item.get("review_status") == "approved"),
        "rejected_count": sum(1 for item in candidates if item.get("review_status") == "rejected"),
        "promoted_count": 0,
        "promotion_policy": "never_auto_promote",
        "refs": {
            "candidates": str(paths.candidates_jsonl),
            "review_queue": str(paths.review_queue_jsonl),
            "decisions": str(paths.decisions_jsonl),
            # LLM: expose the future export ledger without writing it during review.
            "exports": str(paths.exports_jsonl),
        },
        "updated_at": str(reviewed.get("reviewed_at", "")),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _review_queue_records 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 review queue records 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _review_queue_records(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "version": 1,
            "candidate_id": item["candidate_id"],
            "candidate_type": item["candidate_type"],
            "gate_status": item["gate_status"],
            "promotion_status": item["promotion_status"],
            "review_status": item.get("review_status", "pending"),
            "review_required": item["review_required"],
            "missing_requirements": list(item.get("missing_requirements", []) or []),
            "source_refs": dict(item.get("source_refs", {}) or {}),
        }
        for item in candidates
    ]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _merge_checkpoint_from_review 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge checkpoint from review 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_checkpoint_from_review(
    checkpoint_path: Path,
    paths: MemoryGateResult,
    candidates: list[dict[str, object]],
) -> None:
    checkpoint = _read_json_object(checkpoint_path)
    memory_gate = dict(checkpoint.get("memory_gate", {}) if isinstance(checkpoint.get("memory_gate"), dict) else {})
    memory_gate.update(
        {
            "status": "reviewed" if any(not item.get("review_required") for item in candidates) else "review_required",
            "candidate_count": len(candidates),
            "candidates_ref": str(paths.candidates_jsonl),
            "review_queue_ref": str(paths.review_queue_jsonl),
            "decisions_ref": str(paths.decisions_jsonl),
            "exports_ref": str(paths.exports_jsonl),
            "skill_spark_gate_ref": str(paths.skill_spark_gate_json),
            "auto_promote": False,
        },
    )
    checkpoint["memory_gate"] = memory_gate
    _write_json(checkpoint_path, checkpoint)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _read_json_object 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 read json object 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _read_jsonl 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 read jsonl 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write json 相关记录，集中处理目标路径、格式化和状态更新。
def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_jsonl 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write jsonl 相关记录，集中处理目标路径、格式化和状态更新。
def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records)
    path.write_text((content + "\n") if content else "", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append_jsonl 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append jsonl 相关记录，集中处理目标路径、格式化和状态更新。
def _append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _utc_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc iso 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = ["MemoryGateReviewRequest", "MemoryGateReviewResult", "record_memory_gate_review"]
