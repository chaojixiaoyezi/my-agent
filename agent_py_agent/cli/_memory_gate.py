
from __future__ import annotations

"""CLI for reviewing run-local memory gate candidates.

给人看的解释：
这个命令默认只写回 review decision；后续导出必须通过显式 mode 参数触发。
"""

from pathlib import Path

from ..agent.memory_archive.memory_gate.export import MemoryGateExportRequest
from ..agent.memory_archive.memory_gate.retention import MemoryGateRetentionRequest
from ..agent.memory_archive.memory_gate.review import MemoryGateReviewRequest
from .common import make_agent
from .models import SubagentsMemoryGateOptions


def cmd_subagents_memory_gate(args) -> int:
    agent = make_agent(args)
    options = _subagents_memory_gate_options(args, agent=agent)
    if options.retention_dry_run or options.retention_apply:
        return _cmd_memory_gate_retention(agent, options)
    if options.export_memory:
        return _cmd_memory_gate_export_memory(agent, options)
    if options.export_skill:
        return _cmd_memory_gate_export_skill(agent, options)
    if options.verify:
        return _cmd_memory_gate_verify(agent, options)
    if options.candidate_id:
        result = agent.subagents.review_memory_gate_candidate(
            options.run_id,
            MemoryGateReviewRequest(
                candidate_id=options.candidate_id,
                decision=options.decision,
                reviewer=options.reviewer,
                note=options.note,
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

    candidates = agent.subagents.list_memory_gate_candidates(options.run_id)
    print("SUBAGENT MEMORY GATE")
    print(f"run_id={options.run_id} total={len(candidates)}")
    if not candidates:
        print("暂时没有 memory gate 候选。")
        return 0
    for item in candidates[: options.limit]:
        print(
            f"- {item.get('candidate_id', '')} type={item.get('candidate_type', '')} "
            f"review={item.get('review_status', 'pending')} "
            f"promotion={item.get('promotion_status', 'not_promoted')} :: "
            f"{str(item.get('content', ''))[:120]}"
        )
    return 0


def _subagents_memory_gate_options(args, *, agent=None) -> SubagentsMemoryGateOptions:
    requested_path = getattr(args, "memory_path", None)
    memory_path = Path(requested_path) if isinstance(requested_path, str) and requested_path else None
    return SubagentsMemoryGateOptions(
        run_id=getattr(args, "run_id", ""),
        candidate_id=getattr(args, "candidate_id", None) or "",
        decision=getattr(args, "decision", None) or "needs_evidence",
        reviewer=getattr(args, "reviewer", None) or "parent",
        note=getattr(args, "note", None) or "",
        memory_path=memory_path,
        skill_output_dir=getattr(args, "skill_output_dir", None),
        limit=_memory_gate_config_int(agent, args, "limit", "subagent_cli_default_limit"),
        retention_dry_run=bool(getattr(args, "retention_dry_run", False)),
        retention_apply=bool(getattr(args, "retention_apply", False)),
        export_memory=bool(getattr(args, "export_memory", False)),
        export_skill=bool(getattr(args, "export_skill", False)),
        verify=bool(getattr(args, "verify", False)),
    )


def _memory_gate_config_int(agent, args, arg_name: str, config_name: str) -> int:
    value = getattr(args, arg_name, None)
    if value is not None:
        return int(value)
    return int(getattr(getattr(agent, "config", None), config_name, 0) or 0)


def _cmd_memory_gate_retention(agent, options: SubagentsMemoryGateOptions) -> int:
    result = agent.subagents.run_memory_gate_retention(
        options.run_id,
        MemoryGateRetentionRequest(apply=options.retention_apply),
    )
    print("SUBAGENT MEMORY GATE RETENTION")
    print(
        f"mode={result.report.get('mode', '')} "
        f"planned={result.report.get('planned_action_count', 0)} "
        f"active_after={result.report.get('active_after_count', 0)}"
    )
    print(f"report={result.report_json}")
    if options.retention_apply:
        print(f"actions={result.actions_jsonl}")
    return 0


def _cmd_memory_gate_export_memory(agent, options: SubagentsMemoryGateOptions) -> int:
    memory_path = options.memory_path or Path(agent.memory.path)
    result = agent.subagents.export_memory_gate_candidates_to_memory(
        options.run_id,
        request=_export_request(options, memory_path=memory_path),
    )
    print("SUBAGENT MEMORY GATE EXPORT MEMORY")
    print(f"exported={result.exported_count} skipped={result.skipped_count}")
    print(f"memory_path={memory_path}")
    print(f"exports={result.exports_jsonl}")
    return 0


def _cmd_memory_gate_export_skill(agent, options: SubagentsMemoryGateOptions) -> int:
    result = agent.subagents.export_memory_gate_candidates_to_skill_drafts(
        options.run_id,
        request=_export_request(options, output_dir=options.skill_output_dir),
    )
    print("SUBAGENT MEMORY GATE EXPORT SKILL DRAFT")
    print(f"exported={result.exported_count} skipped={result.skipped_count}")
    for item in result.exported[: options.limit]:
        print(f"- draft={item.get('draft_path', '')}")
    print(f"exports={result.exports_jsonl}")
    return 0


def _cmd_memory_gate_verify(agent, options: SubagentsMemoryGateOptions) -> int:
    result = agent.subagents.verify_memory_gate_boundary(options.run_id)
    print("SUBAGENT MEMORY GATE VERIFY")
    print(f"ok={result.ok} problems={result.report.get('problem_count', 0)}")
    print(f"report={result.report_json}")
    return 0 if result.ok else 1


def _export_request(
    options: SubagentsMemoryGateOptions,
    *,
    memory_path=None,
    output_dir=None,
) -> MemoryGateExportRequest:
    return MemoryGateExportRequest(
        candidate_id=options.candidate_id,
        reviewer=options.reviewer,
        memory_path=memory_path,
        output_dir=output_dir,
    )
