from __future__ import annotations

"""LLM: build readiness, channel, and structured-output findings.

新手说明:
检查任务是否处于等待验收状态、通道是否正常、runner 结构化输出是否可解析。
"""

from ..models import SubAgentTask
from ..reports import AcceptanceReviewFinding
from .evidence import _make_finding


def _structured_output_message(
    runner: dict[str, object],
    found: bool,
    ok: bool,
) -> str:
    """Build message string for structured output finding."""
    if found and ok:
        return "runner 结构化输出可解析。"
    if not found:
        return "runner 未记录结构化输出，按人工证据验收。"
    return f"runner 结构化输出解析失败: {runner.get('structured_parse_error', '')}"


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
        _make_finding(
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
        _make_finding(
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
        _make_finding(
            name="structured_output",
            ok=(not runner_structured_found) or runner_structured_ok,
            severity="P1",
            message=_structured_output_message(runner, runner_structured_found, runner_structured_ok),
            evidence_path=task.runner_result_json,
            created_at=created_at,
        )
    )
    return findings
