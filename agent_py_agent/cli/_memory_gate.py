from __future__ import annotations

"""LLM: CLI for reviewing run-local memory gate candidates.

给人看的解释：
这个命令默认只写回 review decision；后续导出必须通过显式 mode 参数触发。
"""

from pathlib import Path

from ..agent.memory_archive.memory_gate_export import MemoryGateExportRequest
from ..agent.memory_archive.memory_gate_retention import MemoryGateRetentionRequest
from ..agent.memory_archive.memory_gate_review import MemoryGateReviewRequest
from .common import make_agent


def cmd_subagents_memory_gate(args) -> int:
    agent = make_agent(args)
    if getattr(args, "retention_dry_run", False) is True or getattr(args, "retention_apply", False) is True:
        return _cmd_memory_gate_retention(agent, args)
    if getattr(args, "export_memory", False) is True:
        return _cmd_memory_gate_export_memory(agent, args)
    if getattr(args, "export_skill", False) is True:
        return _cmd_memory_gate_export_skill(agent, args)
    if getattr(args, "verify", False) is True:
        return _cmd_memory_gate_verify(agent, args)
    if getattr(args, "candidate_id", ""):
        result = agent.subagents.review_memory_gate_candidate(
            args.run_id,
            MemoryGateReviewRequest(
                candidate_id=args.candidate_id,
                decision=args.decision,
                reviewer=args.reviewer,
                note=args.note or "",
            ),
        )
        candidate = result.candidate
        print("SUBAGENT MEMORY GATE REVIEW")
        print(
            f"candidate_id={candidate.get('candidate_id', '')} "
            f"decision={candidate.get('review_decision', '')} "
            f"promotion_status={candidate.get('promotion_status', '')}"
        )
        print(f"decision_log={result.decisions_jsonl}")
        print(f"gate_summary={result.skill_spark_gate_json}")
        return 0

    candidates = agent.subagents.list_memory_gate_candidates(args.run_id)
    print("SUBAGENT MEMORY GATE")
    print(f"run_id={args.run_id} total={len(candidates)}")
    if not candidates:
        print("暂时没有 memory gate 候选。")
        return 0
    for item in candidates[: args.limit]:
        print(
            f"- {item.get('candidate_id', '')} type={item.get('candidate_type', '')} "
            f"review={item.get('review_status', 'pending')} "
            f"promotion={item.get('promotion_status', 'not_promoted')} :: "
            f"{str(item.get('content', ''))[:120]}"
        )
    return 0


def _cmd_memory_gate_retention(agent, args) -> int:
    result = agent.subagents.run_memory_gate_retention(
        args.run_id,
        MemoryGateRetentionRequest(apply=bool(getattr(args, "retention_apply", False))),
    )
    print("SUBAGENT MEMORY GATE RETENTION")
    print(
        f"mode={result.report.get('mode', '')} "
        f"planned={result.report.get('planned_action_count', 0)} "
        f"active_after={result.report.get('active_after_count', 0)}"
    )
    print(f"report={result.report_json}")
    if getattr(args, "retention_apply", False):
        print(f"actions={result.actions_jsonl}")
    return 0


def _cmd_memory_gate_export_memory(agent, args) -> int:
    requested_path = getattr(args, "memory_path", "")
    memory_path = Path(requested_path if isinstance(requested_path, str) and requested_path else agent.memory.path)
    result = agent.subagents.export_memory_gate_candidates_to_memory(
        args.run_id,
        memory_path=memory_path,
        request=_export_request(args),
    )
    print("SUBAGENT MEMORY GATE EXPORT MEMORY")
    print(f"exported={result.exported_count} skipped={result.skipped_count}")
    print(f"memory_path={memory_path}")
    print(f"exports={result.exports_jsonl}")
    return 0


def _cmd_memory_gate_export_skill(agent, args) -> int:
    result = agent.subagents.export_memory_gate_candidates_to_skill_drafts(
        args.run_id,
        output_dir=getattr(args, "skill_output_dir", None),
        request=_export_request(args),
    )
    print("SUBAGENT MEMORY GATE EXPORT SKILL DRAFT")
    print(f"exported={result.exported_count} skipped={result.skipped_count}")
    for item in result.exported[: args.limit]:
        print(f"- draft={item.get('draft_path', '')}")
    print(f"exports={result.exports_jsonl}")
    return 0


def _cmd_memory_gate_verify(agent, args) -> int:
    result = agent.subagents.verify_memory_gate_boundary(args.run_id)
    print("SUBAGENT MEMORY GATE VERIFY")
    print(f"ok={result.ok} problems={result.report.get('problem_count', 0)}")
    print(f"report={result.report_json}")
    return 0 if result.ok else 1


def _export_request(args) -> MemoryGateExportRequest:
    return MemoryGateExportRequest(
        candidate_id=getattr(args, "candidate_id", "") or "",
        reviewer=getattr(args, "reviewer", "") or "parent",
    )
