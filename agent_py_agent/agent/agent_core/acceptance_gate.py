# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。


from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..subagents.utils import _read_json_object

if TYPE_CHECKING:
    from ..core import SimpleAgent


# ---------------------------------------------------------------------------
# Acceptance evaluation helpers
# ---------------------------------------------------------------------------


# LLM: review_acceptance_summary 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理审查验收summary相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def review_acceptance_summary(agent, limit: int = 20) -> dict[str, Any]:
    tasks = agent.subagents.list_runs()
    needs_acceptance = [
        task
        for task in tasks
        if task.status == "AWAITING_ACCEPTANCE" and task.verification_status == "NEEDS_ACCEPTANCE"
    ]
    return {
        "total": len(needs_acceptance),
        "samples": needs_acceptance[:limit],
    }


# LLM: acceptance_decision_from_evidence 属于 SimpleAgent 核心运行的函数边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 函数用途: 处理来自验收decision证据相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持运行循环、工具调用、调度记录和最终响应上的返回值和副作用边界稳定。
def acceptance_decision_from_evidence(task) -> str:
    # Count passing evidence
    passing_evidence = sum(1 for e in task.evidence if getattr(e, "ok", False))

    # Try to load output.json for test results
    try:
        from pathlib import Path

        output_path = Path(task.output_json)
        if output_path.exists():
            payload = _read_json_object(output_path)
            tests = payload.get("tests", [])
            passing_tests = sum(1 for t in tests if t.get("ok", False))
            total_tests = len(tests)
        else:
            passing_tests = 0
            total_tests = 0
    except Exception:
        passing_tests = 0
        total_tests = 0

    # Decision logic:
    # - If no evidence at all, needs more work
    # - If significant failing evidence, needs work
    # - If tests exist and all pass, approved
    # - If tests exist and some fail, depends on severity
    # - Default: needs work

    if passing_evidence == 0 and total_tests == 0:
        return "NEEDS_WORK"

    if passing_evidence > 0:
        # Check if any evidence is marked as failing
        failing_evidence = sum(1 for e in task.evidence if not getattr(e, "ok", True))
        if failing_evidence > passing_evidence / 2:
            return "REJECTED"

    if total_tests > 0:
        if passing_tests == total_tests:
            return "APPROVED"
        if passing_tests < total_tests / 2:
            return "REJECTED"

    return "NEEDS_WORK"
