from __future__ import annotations

"""LLM: CLI for reviewing run-local memory gate candidates.

给人看的解释：
这个命令只写回 review decision，不会把候选写入主 memory 或正式 skill。
"""

from ..agent.memory_archive.memory_gate_review import MemoryGateReviewRequest
from .common import make_agent


def cmd_subagents_memory_gate(args) -> int:
    agent = make_agent(args)
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
