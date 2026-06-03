
from __future__ import annotations

"""Recovery output file writers for subagent persistence."""

import json
from pathlib import Path

from ....runtime_errors import runtime_error_report
from ...models import SubAgentTask
from ..compact_continue_packet import SubagentContinuePacketRequest, write_subagent_continue_packet
from ..takeover.readiness import write_takeover_readiness_files


def write_recovery_output_files(task: SubAgentTask, checkpoint_artifacts: dict[str, object]) -> None:
    for field_name, artifact_payload in checkpoint_artifacts.items():
        _write_checkpoint_artifact(getattr(task, field_name, ""), artifact_payload)
    write_takeover_readiness_files(task)
    output_payload, output_load_error = _output_payload_report(task)
    write_subagent_continue_packet(
        SubagentContinuePacketRequest(
            task,
            output_payload,
            load_errors=tuple(error for error in (output_load_error,) if error),
        )
    )


def _write_checkpoint_artifact(path_text: str, artifact_payload: object) -> None:
    if not path_text:
        return
    path = Path(path_text)
    if isinstance(artifact_payload, str):
        path.write_text(artifact_payload, encoding="utf-8")
        return
    path.write_text(json.dumps(artifact_payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _output_payload(task: SubAgentTask) -> dict[str, object]:
    payload, _load_error = _output_payload_report(task)
    return payload


def _output_payload_report(task: SubAgentTask) -> tuple[dict[str, object], dict[str, object] | None]:
    if not task.output_json:
        return {}, None
    path = Path(task.output_json)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {}, _output_load_error(path, exc)
    if not isinstance(payload, dict):
        return {}, _output_load_error(path, ValueError(f"output_json is {type(payload).__name__}, expected object"))
    return payload, None


def _output_load_error(path: Path, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context="subagent.continue_packet.output_json")
    report["path"] = str(path)
    return report
