# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""retention planning for run-local memory gate queues.

Human version:
Retention here is deliberately conservative. It can compact the active review
queue, but the full candidate and decision logs stay in place for audit and
takeover. A dry run writes only a report; apply writes an action ledger too.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .memory_gate import memory_gate_paths
from .memory_gate_candidates import memory_gate_review_queue_records, read_memory_gate_jsonl


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 MemoryGateRetentionRequest 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryGateRetentionRequest 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class MemoryGateRetentionRequest:
    """Controls whether retention only previews or updates active queue files."""

    apply: bool = False
    now: float | None = None


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 MemoryGateRetentionResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryGateRetentionResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class MemoryGateRetentionResult:
    """Retention report paths and counters for one agent run."""

    report: dict[str, object]
    report_json: Path
    actions_jsonl: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 run_memory_gate_retention 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 推进 run memory gate retention 对应的调度、执行或处理步骤，并返回可追踪的状态结果。
def run_memory_gate_retention(
    agent_run_workspace_root: Path,
    request: MemoryGateRetentionRequest,
) -> MemoryGateRetentionResult:
    """Plan or apply conservative retention for a run-local memory gate."""

    paths = memory_gate_paths(agent_run_workspace_root)
    paths.gate_dir.mkdir(parents=True, exist_ok=True)
    candidates = read_memory_gate_jsonl(paths.candidates_jsonl)
    closed = [_retention_action(item, request) for item in candidates if _is_closed_candidate(item)]
    closed_ids = {str(item["candidate_id"]) for item in closed}
    active_candidates = [item for item in candidates if str(item.get("candidate_id") or "") not in closed_ids]
    report = _report_payload(candidates, closed, request)
    report_json = paths.gate_dir / "retention_report.json"
    actions_jsonl = paths.gate_dir / "retention_actions.jsonl"
    _write_json(report_json, report)
    if request.apply:
        _write_jsonl(paths.review_queue_jsonl, memory_gate_review_queue_records(active_candidates))
        _append_jsonl_many(actions_jsonl, closed)
        _merge_checkpoint(agent_run_workspace_root / "checkpoint.json", report, report_json, actions_jsonl)
    return MemoryGateRetentionResult(report=report, report_json=report_json, actions_jsonl=actions_jsonl)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _is_closed_candidate 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 is closed candidate 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _is_closed_candidate(candidate: dict[str, object]) -> bool:
    status = str(candidate.get("review_status") or "")
    promotion = str(candidate.get("promotion_status") or "")
    return status == "rejected" or promotion in {"promoted_to_memory", "skill_draft_created"}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _retention_action 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 retention action 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _retention_action(
    candidate: dict[str, object],
    request: MemoryGateRetentionRequest,
) -> dict[str, object]:
    return {
        "version": 1,
        "action": "remove_from_active_review_queue",
        "apply": request.apply,
        "candidate_id": candidate.get("candidate_id", ""),
        "candidate_type": candidate.get("candidate_type", ""),
        "review_status": candidate.get("review_status", ""),
        "promotion_status": candidate.get("promotion_status", ""),
        "reason": "closed_candidate_kept_in_audit_logs",
        "created_at": _utc_iso(request.now),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _report_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 report payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def _report_payload(
    candidates: list[dict[str, object]],
    actions: list[dict[str, object]],
    request: MemoryGateRetentionRequest,
) -> dict[str, object]:
    return {
        "version": 1,
        "mode": "apply" if request.apply else "dry_run",
        "candidate_count": len(candidates),
        "planned_action_count": len(actions),
        "active_after_count": len(candidates) - len(actions),
        "actions": actions,
        "policy": "audit_logs_preserved_active_queue_compacted_only",
        "updated_at": _utc_iso(request.now),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _merge_checkpoint 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge checkpoint 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_checkpoint(
    checkpoint_path: Path,
    report: dict[str, object],
    report_json: Path,
    actions_jsonl: Path,
) -> None:
    checkpoint = _read_json_object(checkpoint_path)
    memory_gate = dict(checkpoint.get("memory_gate", {}) if isinstance(checkpoint.get("memory_gate"), dict) else {})
    memory_gate["retention"] = {
        "mode": report.get("mode", ""),
        "planned_action_count": report.get("planned_action_count", 0),
        "report_ref": str(report_json),
        "actions_ref": str(actions_jsonl),
        "destructive_delete": False,
    }
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append_jsonl_many 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append jsonl many 相关记录，集中处理目标路径、格式化和状态更新。
def _append_jsonl_many(path: Path, records: list[dict[str, object]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _utc_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc iso 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _utc_iso(value: float | None) -> str:
    timestamp = value if value is not None else datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


__all__ = [
    "MemoryGateRetentionRequest",
    "MemoryGateRetentionResult",
    "run_memory_gate_retention",
]
