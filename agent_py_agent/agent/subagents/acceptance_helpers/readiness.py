from __future__ import annotations

"""LLM: build readiness, channel, and structured-output findings.

新手说明:
检查任务是否处于等待验收状态、通道是否正常、runner 结构化输出是否可解析。
"""

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding


def _build_readiness_findings(
    task: SubAgentTask,
    runner: dict[str, object],
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """LLM: build readiness, channel, and structured-output findings.

    新手说明:
    检查任务是否处于等待验收状态、通道是否正常、runner 结构化输出是否可解析。
    """

    findings: list[AcceptanceReviewFinding] = []

    ready = task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE"
    findings.append(
        AcceptanceReviewFinding(
            name="ready_for_acceptance",
            ok=ready,
            severity="P1",
            message=(
                "任务处于等待验收状态。"
                if ready
                else f"任务未处于等待验收状态: status={task.status} verify={task.verification_status}"
            ),
            evidence_path=task.runner_result_json,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="channel_not_broken",
            ok=task.channel_status != "BROKEN",
            severity="P1",
            message=(
                "通道未标记为 BROKEN。"
                if task.channel_status != "BROKEN"
                else "通道为 BROKEN，不能验收。"
            ),
            evidence_path=task.channel_probe_file,
            created_at=created_at,
        )
    )

    runner_structured_found = bool(runner.get("structured_output_found", False))
    runner_structured_ok = bool(runner.get("structured_output_ok", False))
    findings.append(
        AcceptanceReviewFinding(
            name="structured_output",
            ok=(not runner_structured_found) or runner_structured_ok,
            severity="P1",
            message=(
                "runner 结构化输出可解析。"
                if runner_structured_found and runner_structured_ok
                else "runner 未记录结构化输出，按人工证据验收。"
                if not runner_structured_found
                else f"runner 结构化输出解析失败: {runner.get('structured_parse_error', '')}"
            ),
            evidence_path=task.runner_result_json,
            created_at=created_at,
        )
    )
    return findings
