# LLM: Real task recovery packets turn failed runs into refs-first continuation facts.
# 模块用途: 为真实主代理任务失败/超时写结构化恢复包，后续续跑只读 refs 和验收 findings。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .main_agent_real_task_acceptance import RealTaskAcceptanceReport
from .main_agent_real_task_suite import MainAgentRealTaskCasePlan
from .recovery_actions import (
    ACTION_NONE,
    ACTION_REPAIR_THEN_RESUME_SAME_CASE,
    ACTION_RESUME_SAME_CASE_AFTER_IDLE_TIMEOUT,
    ACTION_RESUME_SAME_CASE_AFTER_TIMEOUT,
)
from .state_machine import normalize_status

SCHEMA_VERSION = "main-agent-real-task-recovery.v1"


# LLM: RealTaskRecoveryPacketRequest bundles everything needed to write one packet.
# 类用途: 描述失败任务的状态、原因、引用路径和验收报告，避免从 stdout 自然语言猜恢复点。
@dataclass(frozen=True)
class RealTaskRecoveryPacketRequest:
    case_id: str
    title: str
    status: str
    exit_code: int | None
    duration_seconds: float
    reason_codes: tuple[str, ...]
    workspace_root: Path
    packet_path: Path
    refs: dict[str, Path | str]
    acceptance: RealTaskAcceptanceReport


# LLM: write_real_task_recovery_packet emits a machine-readable continuation packet.
# 函数用途: 写 recovery_packet.json；包里只放结构化原因、路径引用和验收 findings 摘要。
def write_real_task_recovery_packet(request: RealTaskRecoveryPacketRequest) -> str:
    payload = _packet_payload(request)
    request.packet_path.parent.mkdir(parents=True, exist_ok=True)
    request.packet_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return _rel(request.packet_path, request.workspace_root)


# LLM: real_task_recovery_refs collects durable per-case refs for continuation.
# 函数用途: 输出 prompt、合同、日志、事件和工作区引用；调用方不需要内联 stdout 或任务正文。
def real_task_recovery_refs(
    paths: dict[str, Path],
    case: MainAgentRealTaskCasePlan,
) -> dict[str, Path | str]:
    return {
        "task_workspace_ref": paths["workspace"],
        "prompt_ref": case.prompt_ref,
        "acceptance_ref": case.acceptance_ref,
        "expected_artifacts_ref": case.expected_artifacts_ref,
        "config_ref": paths["config"],
        "command_ref": paths["command"],
        "delivery_contract_ref": paths["delivery_contract"],
        "stdout_ref": paths["stdout"],
        "stderr_ref": paths["stderr"],
        "events_ref": paths["events"],
        "acceptance_report_ref": paths["acceptance_report"],
    }


# LLM: _packet_payload keeps recovery semantics stable across CLI and future task cards.
# 函数用途: 生成恢复包字段；recommended_action 是机器动作码，不承载任务事实。
def _packet_payload(request: RealTaskRecoveryPacketRequest) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": request.case_id,
        "title": request.title,
        "status": request.status,
        "recovery_required": normalize_status(request.status) != "DONE",
        "recommended_action": _recommended_action(request),
        "exit_code": request.exit_code,
        "duration_seconds": round(request.duration_seconds, 3),
        "reason_codes": list(request.reason_codes),
        "refs": _refs_payload(request.refs, request.workspace_root),
        "acceptance": _acceptance_payload(request.acceptance),
    }


# LLM: _recommended_action maps structured outcomes to stable runtime action codes.
# 函数用途: 根据状态和原因码返回续跑动作码；不检查 stdout 或用户 prompt 内容。
def _recommended_action(request: RealTaskRecoveryPacketRequest) -> str:
    if normalize_status(request.status) == "DONE":
        return ACTION_NONE
    if "activity_timeout" in request.reason_codes:
        return ACTION_RESUME_SAME_CASE_AFTER_IDLE_TIMEOUT
    if "timeout" in request.reason_codes:
        return ACTION_RESUME_SAME_CASE_AFTER_TIMEOUT
    return ACTION_REPAIR_THEN_RESUME_SAME_CASE


# LLM: _refs_payload normalizes all recovery anchors to workspace-relative refs.
# 函数用途: 输出 stdout/stderr、验收报告、命令、工作区等引用，避免内联大文件正文。
def _refs_payload(refs: dict[str, Path | str], workspace_root: Path) -> dict[str, str]:
    return {
        str(key): _rel(value, workspace_root) if isinstance(value, Path) else str(value)
        for key, value in refs.items()
        if str(value)
    }


# LLM: _acceptance_payload preserves artifact findings as structured repair inputs.
# 函数用途: 提取 failed artifact、finding code 和 runtime finding；后续 repair 不需要读自然语言日志。
def _acceptance_payload(acceptance: RealTaskAcceptanceReport) -> dict[str, object]:
    return {
        "summary": dict(acceptance.summary),
        "artifact_failures": [
            _artifact_failure(item.to_dict())
            for item in acceptance.artifacts
            if not item.ok
        ],
        "runtime_findings": [dict(finding) for finding in acceptance.runtime_findings],
    }


# LLM: _artifact_failure keeps only bounded validator evidence for one failed artifact.
# 函数用途: 从 artifact report 提取失败代码和位置值，避免把整个产物或长日志塞进恢复包。
def _artifact_failure(item: dict[str, object]) -> dict[str, object]:
    report = item.get("report")
    report_payload = report if isinstance(report, dict) else {}
    return {
        "artifact_id": item.get("artifact_id") or "",
        "path": item.get("path") or "",
        "validator": item.get("validator") or "",
        "finding_codes": _finding_codes(report_payload),
        "findings": _bounded_findings(report_payload),
    }


# LLM: _finding_codes returns compact machine issue labels for dashboards.
# 函数用途: 从结构化 findings 列表里提取 code；不解析 message 或自然语言说明。
def _finding_codes(report: dict[str, object]) -> list[str]:
    return [str(item.get("code") or "") for item in _findings(report) if item.get("code")]


# LLM: _bounded_findings keeps recovery packets small but actionable.
# 函数用途: 保留前十条结构化 finding 的 code/location/value，供续跑先修关键问题。
def _bounded_findings(report: dict[str, object]) -> list[dict[str, object]]:
    bounded: list[dict[str, object]] = []
    for item in _findings(report)[:10]:
        bounded.append(
            {
                "code": item.get("code") or "",
                "location": item.get("location") or "",
                "value": item.get("value") or "",
            }
        )
    return bounded


# LLM: _findings hides report shape normalization from packet assembly.
# 函数用途: 返回 artifact validator 的结构化 findings；坏形状按空列表处理。
def _findings(report: dict[str, object]) -> list[dict[str, object]]:
    findings = report.get("findings")
    if not isinstance(findings, list):
        return []
    return [dict(item) for item in findings if isinstance(item, dict)]


# LLM: _rel makes packet refs portable inside the real-task workspace.
# 函数用途: 将绝对路径转成工作区相对路径；外部路径才保留绝对值。
def _rel(path: Path | str, base: Path) -> str:
    candidate = Path(path)
    try:
        return str(candidate.relative_to(base))
    except ValueError:
        return str(candidate)


__all__ = [
    "RealTaskRecoveryPacketRequest",
    "SCHEMA_VERSION",
    "real_task_recovery_refs",
    "write_real_task_recovery_packet",
]
