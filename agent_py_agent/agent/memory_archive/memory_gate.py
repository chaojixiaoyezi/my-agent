# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""review gate files for task-local memory and skill-spark candidates.

Human version:
Subagents can produce useful lessons and findings, but those facts must not
jump straight into main long-term memory or formal skills. This module writes a
small run-local gate queue so a parent/verifier can review evidence, scope, and
limits before any later promotion workflow.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_gate_candidates import (
    build_memory_gate_candidates,
    memory_gate_review_queue_records,
    read_memory_gate_jsonl,
)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 MemoryGateResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryGateResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class MemoryGateResult:
    """Concrete files for one run-local memory promotion gate."""

    gate_dir: Path
    candidates_jsonl: Path
    review_queue_jsonl: Path
    decisions_jsonl: Path
    # LLM: exports stay separate from review decisions so approve cannot masquerade as promotion.
    exports_jsonl: Path
    skill_spark_gate_json: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 sync_agent_run_memory_gate 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 sync agent run memory gate 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def sync_agent_run_memory_gate(
    task: Any,
    *,
    agent_run_workspace_root: Path,
    now: float,
) -> MemoryGateResult:
    """Write gated memory/skill candidate records for one subagent run."""

    paths = memory_gate_paths(agent_run_workspace_root)
    paths.gate_dir.mkdir(parents=True, exist_ok=True)
    # LLM: candidate extraction is split out so this adapter stays below code-size guardrails.
    candidates = build_memory_gate_candidates(task, now=now, existing_candidates_jsonl=paths.candidates_jsonl)
    _write_jsonl(paths.candidates_jsonl, candidates)
    _write_jsonl(paths.review_queue_jsonl, memory_gate_review_queue_records(candidates))
    _write_json(paths.skill_spark_gate_json, _gate_summary(task, paths, candidates, now))
    _merge_checkpoint(agent_run_workspace_root / "checkpoint.json", paths, candidates)
    return paths


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 memory_gate_paths 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 memory gate paths 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def memory_gate_paths(agent_run_workspace_root: Path) -> MemoryGateResult:
    """Return memory gate paths without writing files."""

    gate_dir = agent_run_workspace_root / "memory_gate"
    return MemoryGateResult(
        gate_dir=gate_dir,
        candidates_jsonl=gate_dir / "candidates.jsonl",
        review_queue_jsonl=gate_dir / "review_queue.jsonl",
        decisions_jsonl=gate_dir / "decisions.jsonl",
        exports_jsonl=gate_dir / "exports.jsonl",
        skill_spark_gate_json=gate_dir / "skill_spark_gate.json",
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 list_memory_gate_candidates 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 list memory gate candidates 的候选结果，并按参数完成筛选、排序或数量限制。
def list_memory_gate_candidates(agent_run_workspace_root: Path) -> list[dict[str, object]]:
    """Read current memory-gate candidates for one agent run workspace."""

    return read_memory_gate_jsonl(memory_gate_paths(agent_run_workspace_root).candidates_jsonl)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _gate_summary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 gate summary 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _gate_summary(
    task: Any,
    paths: MemoryGateResult,
    candidates: list[dict[str, object]],
    now: float,
) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": str(getattr(task, "root_id", "") or getattr(task, "id", "task")),
        "run_id": str(getattr(task, "id", "")),
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
            "exports": str(paths.exports_jsonl),
        },
        "updated_at": _utc_iso(now),
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _merge_checkpoint 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 merge checkpoint 涉及的字段，让后续匹配和存储使用同一形态。
def _merge_checkpoint(checkpoint_path: Path, paths: MemoryGateResult, candidates: list[dict[str, object]]) -> None:
    checkpoint = _read_json_object(checkpoint_path)
    checkpoint["memory_gate"] = {
        "status": "review_required",
        "candidate_count": len(candidates),
        "candidates_ref": str(paths.candidates_jsonl),
        "review_queue_ref": str(paths.review_queue_jsonl),
        "decisions_ref": str(paths.decisions_jsonl),
        "exports_ref": str(paths.exports_jsonl),
        "skill_spark_gate_ref": str(paths.skill_spark_gate_json),
        "auto_promote": False,
    }
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


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _utc_iso 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 utc iso 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _utc_iso(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


__all__ = [
    "MemoryGateResult",
    "list_memory_gate_candidates",
    "memory_gate_paths",
    "sync_agent_run_memory_gate",
]
