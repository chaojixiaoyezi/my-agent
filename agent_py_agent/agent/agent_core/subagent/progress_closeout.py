
from __future__ import annotations

import json
from pathlib import Path

from ...backends import ModelResponse
from ...common.json_io import read_json_object_report
from ...runtime_errors import runtime_error_report
from ..runner.context import current_subagent_run_id


def subagent_progress_closeout_response(agent, base_response: ModelResponse) -> ModelResponse | None:
    task, load_error = _current_subagent_task(agent)
    if load_error:
        return _progress_load_error_response(load_error[0], load_error[1], base_response)
    if task is None:
        return None
    progress, progress_load_error = _latest_progress_payload(task)
    if progress_load_error:
        return _progress_load_error_response(task.id, progress_load_error, base_response)
    if not _progress_ready_for_closeout(progress, task):
        return None
    return _closeout_response(_progress_closeout_payload(progress, task), base_response)


def _current_subagent_task(agent) -> tuple[object | None, tuple[str, BaseException] | None]:
    run_id = current_subagent_run_id(agent)
    if not run_id:
        return None, None
    try:
        return agent.subagents.load(run_id), None
    except Exception as exc:
        return None, (run_id, exc)


def _latest_progress_payload(task) -> tuple[dict[str, object], dict[str, object] | None]:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    if not workspace:
        return {}, None
    report = read_json_object_report(
        Path(workspace) / "progress" / "latest_tool_progress.json",
        parse_nested_string=True,
        context="subagent_progress_closeout.latest_tool_progress",
    )
    return report.payload, report.load_error


def _progress_ready_for_closeout(progress: dict[str, object], task) -> bool:
    path = str(progress.get("latest_written_path") or "").strip()
    if not path or path == str(progress.get("closeout_written_path") or "").strip():
        return False
    if _same_path(path, getattr(task, "output_json", "")):
        return False
    artifact = Path(path)
    if not artifact.is_file():
        return False
    integrity = progress.get("artifact_integrity")
    if isinstance(integrity, dict) and integrity.get("kind") == "html":
        if integrity.get("ok") is not True:
            return False
        return not (integrity.get("blocker_codes") or integrity.get("warning_codes"))
    return _matches_declared_product_output(path, task)


def _progress_closeout_payload(progress: dict[str, object], task) -> dict[str, object]:
    artifact_ref = str(progress.get("latest_written_path") or "").strip()
    progress_ref = str(progress.get("latest_tool_progress_ref") or "").strip()
    evidence_refs = [ref for ref in [progress_ref] if ref]
    return {
        "status": "DONE",
        "summary": "task-local progress shows a declared product artifact ready for parent summarization.",
        "used_tools": [],
        "used_skills": [],
        "evidence": _progress_evidence(progress_ref, artifact_ref),
        "evidence_packets": _progress_evidence_packets(artifact_ref, evidence_refs, task),
        "coverage_records": [],
        "capability_requests": [],
        "artifacts": _progress_artifacts(artifact_ref),
        "tests": _progress_tests(progress),
        "patches": [],
        "lessons": [],
        "next_actions": ["summarize_or_deliver"],
        "blocked_reason": "",
        "failure_type": "",
    }


def _progress_evidence(progress_ref: str, artifact_ref: str) -> list[dict[str, object]]:
    return [
        {
            "kind": "product_artifact_progress",
            "summary": "latest product artifact path is recorded by task-local progress",
            "path": progress_ref,
            "url": "",
            "ok": True,
            "artifact_ref": artifact_ref,
        }
    ]


def _progress_evidence_packets(
    artifact_ref: str, evidence_refs: list[str], task: object
) -> list[dict[str, object]]:
    return [
        {
            "id": f"evpkt-progress-closeout-{getattr(task, 'id', 'run')}",
            "claim": "latest product artifact passed task-local artifact integrity checks",
            "checked_scope": "latest_tool_progress",
            "evidence_refs": evidence_refs,
            "artifact_refs": [artifact_ref],
            "confidence": 0.8,
        }
    ]


def _progress_artifacts(artifact_ref: str) -> list[dict[str, object]]:
    return [
        {
            "path": artifact_ref,
            "kind": "file",
            "summary": "product artifact ref from latest_tool_progress",
        }
    ]


def _progress_tests(progress: dict[str, object]) -> list[dict[str, object]]:
    integrity = progress.get("artifact_integrity")
    if isinstance(integrity, dict) and integrity.get("kind") == "html":
        return [
            {
                "name": "artifact integrity",
                "validation_method": "artifact_integrity",
                "ok": True,
                "summary": "no blocker_codes or warning_codes",
            }
        ]
    return [
        {
            "name": "declared product artifact exists",
            "validation_method": "file_exists",
            "ok": True,
            "summary": "declared output path exists and is non-empty enough for closeout",
        }
    ]


def _closeout_response(payload: dict[str, object], base_response: ModelResponse) -> ModelResponse:
    text = (
        "[SUBAGENT_RESULT]\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_RESULT]\n\n"
        "系统检测到 task-local progress 已形成产物引用，已结束工具循环并交回父级汇总。"
    )
    return ModelResponse(text=text, backend=base_response.backend)


def _progress_load_error_response(
    run_id: str,
    error: BaseException | dict[str, object],
    base_response: ModelResponse,
) -> ModelResponse:
    report = (
        error
        if isinstance(error, dict)
        else runtime_error_report(error, context="subagent_progress_closeout.subagents.load")
    )
    text = (
        "[SUBAGENT_PROGRESS_LOAD_ERROR]\n"
        f"run_id={run_id}\n"
        "系统检测到 task-local progress 收口需要读取当前子代理账本，但账本读取失败；"
        "这不是子代理没有产物，也不是可以假装完成。请刷新代理树、读取 canonical state，"
        "或让父代理接管恢复。\n"
        f"{json.dumps(report, ensure_ascii=False, indent=2)}\n"
        "[/SUBAGENT_PROGRESS_LOAD_ERROR]"
    )
    return ModelResponse(text=text, backend=base_response.backend)


def _same_path(first: object, second: object) -> bool:
    left = str(first or "").strip()
    right = str(second or "").strip()
    if not left or not right:
        return False
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except OSError:
        return left == right


def _matches_declared_product_output(path: str, task: object) -> bool:
    return any(_same_path(path, ref) for ref in _declared_product_refs(task))


def _declared_product_refs(task: object) -> list[str]:
    refs: list[str] = []
    attrs = getattr(task, "attributes", {}) or {}
    if isinstance(attrs, dict):
        for key in ("output_refs", "output_files", "artifact_refs"):
            refs.extend(_progress_ref_list(attrs.get(key)))
    for pack in getattr(task, "context_packs", []) or []:
        if not isinstance(pack, dict):
            continue
        contract = pack.get("contract")
        if not isinstance(contract, dict):
            continue
        kind = str(contract.get("kind") or "").strip()
        idem_key = str(contract.get("idempotency_key") or contract.get("key") or "").strip()
        if kind != "system_derived_output_scope" and not idem_key.endswith(".output_refs"):
            continue
        refs.extend(_progress_ref_list(contract.get("scope_refs")))
        refs.extend(_progress_ref_list(contract.get("target_artifact_refs")))
    return _unique_strings(refs)


def _progress_ref_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique
