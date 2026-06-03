
from __future__ import annotations

"""deterministic verifier for the memory gate promotion boundary."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...common.json_io import write_json_object
from . import memory_gate_paths
from .candidates import read_memory_gate_jsonl


@dataclass(frozen=True)
class MemoryGateVerifierResult:
    """Boundary verification result for one run-local gate."""

    ok: bool
    report: dict[str, object]
    report_json: Path


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
    write_json_object(report_json, report, sort_keys=False)
    return MemoryGateVerifierResult(ok=not problems, report=report, report_json=report_json)


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


def _verify_decisions(decisions: list[dict[str, object]]) -> list[str]:
    problems: list[str] = []
    for item in decisions:
        if item.get("auto_promote") is not False:
            problems.append(f"{item.get('candidate_id', '')}: decision auto_promote must be false")
    return problems


def _verify_exports(exports: list[dict[str, object]], candidates: list[dict[str, object]]) -> list[str]:
    by_id = {str(item.get("candidate_id") or ""): item for item in candidates}
    problems: list[str] = []
    for row in exports:
        problem = _export_problem(row, by_id.get(str(row.get("candidate_id") or "")))
        if problem:
            problems.append(problem)
    return problems


def _export_problem(row: dict[str, object], candidate: dict[str, object] | None) -> str:
    candidate_id = str(row.get("candidate_id") or "")
    if not candidate:
        return f"{candidate_id}: export references missing candidate"
    expected = {"memory": "promoted_to_memory", "skill_draft": "skill_draft_created"}.get(str(row.get("export_type") or ""))
    if expected and candidate.get("promotion_status") != expected:
        return f"{candidate_id}: {row.get('export_type', '')} export status mismatch"
    return ""


def _verify_files(paths) -> list[str]:
    required = [paths.gate_dir, paths.candidates_jsonl, paths.review_queue_jsonl, paths.skill_spark_gate_json]
    return [f"missing required gate path: {path}" for path in required if not Path(path).exists()]


__all__ = ["MemoryGateVerifierResult", "verify_memory_gate_boundary"]
