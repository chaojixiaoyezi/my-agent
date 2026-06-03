from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ...backends import ModelResponse
from ...common.json_io import read_json_object_report
from ...runtime_errors import runtime_error_report
from ...tools import ToolExecutionResult
from .._runtime_params import ToolLoopExecuteParams
from ..runner.context import current_subagent_run_id


@dataclass(frozen=True)
class SubagentOutputWriteCheck:
    agent: object
    params: ToolLoopExecuteParams
    payload: object
    result: ToolExecutionResult


def subagent_output_json_response(agent, fallback: ModelResponse) -> ModelResponse:
    run_id = current_subagent_run_id(agent)
    try:
        task = agent.subagents.load(run_id)
    except Exception as exc:
        return ModelResponse(
            text=_subagent_output_load_error_text(run_id, exc),
            backend=fallback.backend,
        )
    report = read_json_object_report(
        Path(task.output_json),
        parse_nested_string=True,
        context="subagent_output_json.output_json",
    )
    if report.load_error:
        return ModelResponse(
            text=_subagent_output_json_load_error_text(task.output_json, report.load_error),
            backend=fallback.backend,
        )
    payload = report.payload
    payload = _enrich_subagent_output_payload(payload, task)
    text = (
        "[SUBAGENT_RESULT]\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_RESULT]\n\n"
        "系统检测到当前子代理已写出 output.json，已结束工具循环并交回父级汇总。"
    )
    return ModelResponse(text=text, backend=fallback.backend)


def is_subagent_output_json_write(check: SubagentOutputWriteCheck) -> bool:
    if not (
        check.result.ok
        and check.result.tool == "write_file"
        and isinstance(check.payload, dict)
    ):
        return False
    run_id = current_subagent_run_id(check.agent)
    if not run_id:
        return False
    try:
        task = check.agent.subagents.load(run_id)
    except Exception as exc:
        _append_subagent_output_load_warning(check.params, run_id, exc)
        return False
    return _same_path(_tool_payload_path(check.payload), getattr(task, "output_json", ""))


def _enrich_subagent_output_payload(payload: dict[str, object], task) -> dict[str, object]:
    if _has_traceable_evidence_packets(payload) or _has_malformed_evidence_packets(payload):
        return payload
    evidence_refs, artifact_refs = _derive_output_json_refs(payload, task)
    if not evidence_refs and not artifact_refs:
        return payload
    enriched = dict(payload)
    enriched["evidence_packets"] = [
        {
            "id": _evidence_packet_id(task),
            "claim": _evidence_packet_claim(enriched),
            "checked_scope": "output_json closeout refs",
            "evidence_refs": evidence_refs[:8],
            "artifact_refs": artifact_refs[:8],
            "confidence": 0.7,
        }
    ]
    _write_enriched_output_json(task, enriched)
    return enriched


def _has_traceable_evidence_packets(payload: dict[str, object]) -> bool:
    packets = payload.get("evidence_packets")
    if not isinstance(packets, list):
        return False
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        if _string_refs(packet.get("evidence_refs")) or _string_refs(packet.get("artifact_refs")):
            return True
    return False


def _has_malformed_evidence_packets(payload: dict[str, object]) -> bool:
    packets = payload.get("evidence_packets")
    if not isinstance(packets, list) or not packets:
        return False
    return not _has_traceable_evidence_packets(payload)


def _derive_output_json_refs(payload: dict[str, object], task) -> tuple[list[str], list[str]]:
    artifact_refs = _payload_path_refs(payload.get("artifacts"))
    evidence_refs = _payload_path_refs(payload.get("evidence"))
    evidence_refs.extend(_task_report_refs(task))
    if artifact_refs or evidence_refs:
        output_ref = _existing_path(getattr(task, "output_json", ""))
        if output_ref:
            evidence_refs.append(output_ref)
    return _unique_strings(evidence_refs), _unique_strings(artifact_refs)


def _payload_path_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        refs.extend(_payload_item_refs(item))
    return refs


def _payload_item_refs(item: object) -> list[str]:
    if not isinstance(item, dict):
        return []
    refs: list[str] = []
    for key in ("path", "file_path", "url"):
        ref = _ref_string(item.get(key))
        if ref:
            refs.append(ref)
    return refs


def _task_report_refs(task) -> list[str]:
    reports_dir_text = str(getattr(task, "reports_dir", "") or "").strip()
    if not reports_dir_text:
        return []
    reports_dir = Path(reports_dir_text)
    if not reports_dir.is_dir():
        return []
    names = (
        "coordinator_report.md",
        "runner_result.json",
        "test_execution.json",
        "failure_handoff.json",
        "takeover_readiness.json",
    )
    return [ref for name in names if (ref := _existing_path(reports_dir / name))]


def _ref_string(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    return _existing_path(text)


def _existing_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        path = Path(text).expanduser()
        return str(path) if path.exists() else ""
    except OSError:
        return ""


def _evidence_packet_id(task) -> str:
    run_id = str(getattr(task, "id", "") or "run").strip() or "run"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in run_id)
    return f"evpkt-output-json-{safe[:48]}"


def _evidence_packet_claim(payload: dict[str, object]) -> str:
    summary = str(payload.get("summary") or "").strip()
    if summary:
        return summary[:200]
    status = str(payload.get("status") or "DONE").strip()
    return f"runner wrote output.json closeout with status={status}"


def _write_enriched_output_json(task, payload: dict[str, object]) -> None:
    try:
        Path(task.output_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def _string_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique


def _append_subagent_output_load_warning(
    params: ToolLoopExecuteParams,
    run_id: str,
    exc: BaseException,
) -> None:
    tool_context = getattr(params, "tool_context", None)
    if isinstance(tool_context, list):
        tool_context.append(_subagent_output_load_error_text(run_id, exc))


def _subagent_output_load_error_text(run_id: str, exc: BaseException) -> str:
    report = runtime_error_report(exc, context="subagent_output_json.subagents.load")
    return (
        "[SUBAGENT_RESULT_LOAD_ERROR]\n"
        f"run_id={run_id}\n"
        "系统检测到子代理结果收口需要读取子代理账本，但账本读取失败；"
        "这不是子代理没有写结果，也不是可以假装完成。请刷新代理树、读取 canonical state，"
        "或交给父代理接管恢复。\n"
        f"{json.dumps(report, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_RESULT_LOAD_ERROR]"
    )


def _subagent_output_json_load_error_text(path: str, load_error: dict[str, object]) -> str:
    return (
        "[SUBAGENT_RESULT_LOAD_ERROR]\n"
        f"output_json={path}\n"
        "系统检测到当前子代理写出了 output.json，但这个文件无法作为结构化结果读取；"
        "这不是子代理没有结果，也不能假装完成。请让子代理修复 output.json，"
        "或让父代理读取 checkpoint/summary 后接管恢复。\n"
        f"{json.dumps(load_error, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_RESULT_LOAD_ERROR]"
    )


def _tool_payload_path(payload: dict[str, object]) -> object:
    if payload.get("path"):
        return payload.get("path")
    filesystem = payload.get("filesystem")
    if isinstance(filesystem, dict):
        return filesystem.get("path")
    return ""


def _same_path(left: object, right: object) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    try:
        return Path(left_text).expanduser().resolve() == Path(right_text).expanduser().resolve()
    except OSError:
        return False
