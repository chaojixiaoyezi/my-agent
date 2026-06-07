
from __future__ import annotations

"""finalized subagent runner persistence helpers kept outside the lifecycle mixin."""

import json
from dataclasses import dataclass

from ...subagents.manager_runner_result_payload import RecordRunnerResultParams
from ...subagents.models import SubAgentParsedOutput
from ...subagents.services.subagent_session_compact import (
    subagent_session_compact_payload_from_result,
)
from .params import RecoverySnapshotParams, SubagentFinalizeParams


@dataclass(frozen=True)
class FinalizedRunnerRecordRequest:
    agent: object
    params: SubagentFinalizeParams
    structured: object
    repair_state: dict


@dataclass(frozen=True)
class FinalizedRecoverySnapshotRequest:
    agent: object
    params: SubagentFinalizeParams
    runner_result: object
    repair_state: dict


def parsed_output_from_delivery_complete_response(text: str) -> SubAgentParsedOutput | None:
    """Convert a runtime delivery-closeout success block into runner output."""

    for payload in _delivery_complete_payloads(text):
        if payload.get("ok") is not True:
            continue
        artifacts = _delivery_complete_artifacts(payload)
        if not artifacts:
            continue
        report_ref = str(payload.get("report_ref") or "").strip()
        artifact_refs = [str(item.get("path") or "").strip() for item in artifacts if str(item.get("path") or "").strip()]
        evidence_refs = [report_ref] if report_ref else []
        return SubAgentParsedOutput(
            found=True,
            ok=True,
            status="DONE",
            summary="当前 run 的 delivery closeout 已通过，产物引用来自运行时验收记录。",
            evidence=[
                {
                    "kind": "delivery_closeout",
                    "summary": "runtime delivery closeout accepted current-run artifacts",
                    "ok": True,
                }
            ],
            evidence_packets=[
                {
                    "id": "evpkt-delivery-closeout",
                    "claim": "runtime delivery closeout accepted current-run artifacts",
                    "checked_scope": "MAIN_AGENT_DELIVERY_COMPLETE",
                    "evidence_refs": evidence_refs,
                    "artifact_refs": artifact_refs,
                    "confidence": 1.0,
                }
            ],
            artifacts=artifacts,
            raw_json=payload,
        )
    return None


def record_finalized_runner_result(request: FinalizedRunnerRecordRequest):
    params = request.params
    structured = request.structured
    repair_state = request.repair_state
    return request.agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=params.run_id,
            attempt_id=params.active_attempt_id,
            dry_run=False,
            ok=structured.ok if structured.found else True,
            message=repair_state["message"],
            prompt=repair_state["prompt_for_log"],
            response=repair_state["response_for_log"],
            backend=repair_state["backend_name"],
            tool_rounds=params.result.tool_rounds,
            status="" if structured.found else "DONE",
            verification_status="" if structured.found else "VERIFIED",
            structured_output=structured,
            actual_tools=params.result.executed_tools or [],
            structured_repair_attempted=repair_state["attempted"],
            structured_repair_ok=repair_state["ok"],
            structured_repair_error=repair_state["error"],
            session_compact=_subagent_session_compact_payload(params.result),
        )
    )


def _subagent_session_compact_payload(result: object) -> dict[str, object]:
    return subagent_session_compact_payload_from_result(result)


def _delivery_complete_payloads(text: str) -> list[dict[str, object]]:
    marker_start = "[MAIN_AGENT_DELIVERY_COMPLETE]"
    marker_end = "[/MAIN_AGENT_DELIVERY_COMPLETE]"
    payloads: list[dict[str, object]] = []
    offset = 0
    while True:
        start = text.find(marker_start, offset)
        if start == -1:
            break
        body_start = start + len(marker_start)
        end = text.find(marker_end, body_start)
        if end == -1:
            break
        payload = _json_object(text[body_start:end].strip())
        if payload is not None:
            payloads.append(payload)
        offset = end + len(marker_end)
    return list(reversed(payloads))


def _json_object(text: str) -> dict[str, object] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _delivery_complete_artifacts(payload: dict[str, object]) -> list[dict[str, object]]:
    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return []
    artifacts: list[dict[str, object]] = []
    for item in raw_artifacts:
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        artifact = {
            "path": path,
            "kind": str(item.get("kind") or "file").strip() or "file",
            "ok": True,
            "summary": "accepted by runtime delivery closeout",
        }
        artifact_id = str(item.get("artifact_id") or "").strip()
        if artifact_id:
            artifact["artifact_id"] = artifact_id
        artifacts.append(artifact)
    return artifacts


def write_finalized_recovery_snapshot(request: FinalizedRecoverySnapshotRequest) -> None:
    request.agent._write_subagent_recovery_snapshot(
        params=RecoverySnapshotParams(
            run_id=request.params.run_id,
            user_prompt=request.params.context.goal,
            response_text=str(request.repair_state["message"]),
            backend=str(request.repair_state["backend_name"]),
            status=request.runner_result.status,
            error_code=str(getattr(request.runner_result, "failure_type", "") or ""),
            tool_calls=[
                {"tool": tool_name, "id": f"{request.params.run_id}:{index}", "ok": True}
                for index, tool_name in enumerate(request.params.result.executed_tools or [], start=1)
            ],
        )
    )
