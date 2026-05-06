
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core import SimpleAgent


# ---------------------------------------------------------------------------
# Acceptance evaluation helpers
# ---------------------------------------------------------------------------


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


def acceptance_decision_from_evidence(task) -> str:
    # Count passing evidence
    passing_evidence = sum(1 for e in task.evidence if getattr(e, "ok", False))

    # Try to load output.json for test results
    try:
        import json
        from pathlib import Path

        output_path = Path(task.output_json)
        if output_path.exists():
            payload = json.loads(output_path.read_text(encoding="utf-8"))
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