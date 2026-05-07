# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""deterministic verifier for the memory gate promotion boundary."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .memory_gate import memory_gate_paths
from .memory_gate_candidates import read_memory_gate_jsonl


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 MemoryGateVerifierResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 MemoryGateVerifierResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class MemoryGateVerifierResult:
    """Boundary verification result for one run-local gate."""

    ok: bool
    report: dict[str, object]
    report_json: Path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 verify_memory_gate_boundary 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 verify memory gate boundary 的输入、状态或路径，提前暴露无效数据和越界条件。
def verify_memory_gate_boundary(agent_run_workspace_root: Path) -> MemoryGateVerifierResult:
    """Check that gate files preserve explicit-promotion boundaries."""

    paths = memory_gate_paths(agent_run_workspace_root)
    candidates = read_memory_gate_jsonl(paths.candidates_jsonl)
    decisions = read_memory_gate_jsonl(paths.decisions_jsonl)
    exports = read_memory_gate_jsonl(paths.exports_jsonl)
    problems = _verify_candidates(candidates)
    problems.extend(_verify_decisions(decisions))
    problems.extend(_verify_exports(exports, candidates))
    problems.extend(_verify_files(paths))
    report = {
        "version": 1,
        "ok": not problems,
        "problem_count": len(problems),
        "problems": problems,
        "candidate_count": len(candidates),
        "decision_count": len(decisions),
        "export_count": len(exports),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    report_json = paths.gate_dir / "verifier_report.json"
    _write_json(report_json, report)
    return MemoryGateVerifierResult(ok=not problems, report=report, report_json=report_json)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _verify_candidates 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 verify candidates 的输入、状态或路径，提前暴露无效数据和越界条件。
def _verify_candidates(candidates: list[dict[str, object]]) -> list[str]:
    problems: list[str] = []
    for item in candidates:
        candidate_id = str(item.get("candidate_id") or "")
        if item.get("auto_promote") is True:
            problems.append(f"{candidate_id}: auto_promote must not be true")
        if item.get("promotion_status") == "promoted_to_memory" and not item.get("memory_export_ref"):
            problems.append(f"{candidate_id}: promoted memory candidate missing memory_export_ref")
        if item.get("promotion_status") == "skill_draft_created" and not item.get("skill_draft_ref"):
            problems.append(f"{candidate_id}: skill draft candidate missing skill_draft_ref")
    return problems


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _verify_decisions 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 verify decisions 的输入、状态或路径，提前暴露无效数据和越界条件。
def _verify_decisions(decisions: list[dict[str, object]]) -> list[str]:
    problems: list[str] = []
    for item in decisions:
        if item.get("auto_promote") is not False:
            problems.append(f"{item.get('candidate_id', '')}: decision auto_promote must be false")
    return problems


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _verify_exports 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 verify exports 的输入、状态或路径，提前暴露无效数据和越界条件。
def _verify_exports(exports: list[dict[str, object]], candidates: list[dict[str, object]]) -> list[str]:
    by_id = {str(item.get("candidate_id") or ""): item for item in candidates}
    problems: list[str] = []
    for row in exports:
        problem = _export_problem(row, by_id.get(str(row.get("candidate_id") or "")))
        if problem:
            problems.append(problem)
    return problems


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _export_problem 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 export problem 相关记录，集中处理目标路径、格式化和状态更新。
def _export_problem(row: dict[str, object], candidate: dict[str, object] | None) -> str:
    candidate_id = str(row.get("candidate_id") or "")
    if not candidate:
        return f"{candidate_id}: export references missing candidate"
    expected = {"memory": "promoted_to_memory", "skill_draft": "skill_draft_created"}.get(str(row.get("export_type") or ""))
    if expected and candidate.get("promotion_status") != expected:
        return f"{candidate_id}: {row.get('export_type', '')} export status mismatch"
    return ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _verify_files 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 校验 verify files 的输入、状态或路径，提前暴露无效数据和越界条件。
def _verify_files(paths) -> list[str]:
    # LLM: files may be empty, but the gate directory and core ledgers should exist after sync.
    required = [paths.gate_dir, paths.candidates_jsonl, paths.review_queue_jsonl, paths.skill_spark_gate_json]
    return [f"missing required gate path: {path}" for path in required if not Path(path).exists()]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _write_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write json 相关记录，集中处理目标路径、格式化和状态更新。
def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


__all__ = ["MemoryGateVerifierResult", "verify_memory_gate_boundary"]
