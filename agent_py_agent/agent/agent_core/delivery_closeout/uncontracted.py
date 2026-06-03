from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...artifacts.registry import registry_path
from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
from ..tool_guard.local_progress import reset_local_progress_guard
from .artifacts import _relative_report_ref, _write_report


def uncontracted_task_output_closeout_response(
    request: object,
    workspace_root: Path,
) -> ModelResponse | None:
    artifacts = _current_run_task_output_artifacts(getattr(request, "params", None))
    if not artifacts:
        return None
    params = request.params
    report = {
        "schema_version": "main_agent_delivery_closeout.v1",
        "ok": True,
        "case_id": "",
        "request_id": params.request_id,
        "run_id": params.run_id,
        "task_id": params.task_id,
        "workspace_root": str(workspace_root),
        "canonical_artifact_registry_ref": _relative_report_ref(registry_path(workspace_root), workspace_root),
        "artifacts": artifacts,
        "delivery_mode": "uncontracted_task_output",
        "message_zh": "没有结构化交付合同，但本轮已写入 task output 下的报告类交付物，且模型显式提交验收；主代理停止继续工具循环。",
    }
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    _write_report(workspace_root, report)
    reset_local_progress_guard(request.agent, params)
    return ModelResponse(text=_uncontracted_closeout_text(report), backend=request.backend)


def _current_run_task_output_artifacts(params: ToolLoopExecuteParams | None) -> list[dict[str, Any]]:
    output_dir = _task_output_dir(params)
    if output_dir is None or params is None:
        return []
    artifacts: list[dict[str, Any]] = []
    for record in _successful_write_records(getattr(params, "archive_tool_calls", []) or []):
        artifacts.extend(_task_output_artifacts_from_record(record, output_dir))
    return _unique_artifact_payloads(artifacts)


def _task_output_artifacts_from_record(record: dict[str, Any], output_dir: Path) -> list[dict[str, Any]]:
    return [
        _artifact_payload(record, path)
        for path in _produced_paths(record)
        if _is_task_output_report(path, output_dir)
    ]


def _successful_write_records(records: object) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict) and _successful_write_record(record)]


def _artifact_payload(record: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "artifact_id": str(record.get("call_id") or path.name),
        "kind": path.suffix.lower().lstrip(".") or "file",
        "path": str(path),
        "ok": True,
        "source": "current_run_tool_output",
    }


def _task_output_dir(params: ToolLoopExecuteParams | None) -> Path | None:
    attrs = params.task_attributes if params is not None and isinstance(params.task_attributes, dict) else {}
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("output_dir") or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


def _successful_write_record(record: dict[str, Any]) -> bool:
    if record.get("ok") is False:
        return False
    return str(record.get("tool") or "").strip() in {"write_file", "apply_patch", "run_command", "controlled_exec"}


def _produced_paths(record: dict[str, Any]) -> list[Path]:
    refs = _record_refs(record)
    paths: list[Path] = []
    for ref in dict.fromkeys(refs):
        if "://" in ref:
            continue
        path = Path(ref).expanduser()
        if path.is_absolute():
            paths.append(path.resolve(strict=False))
    return paths


def _record_refs(record: dict[str, Any]) -> list[str]:
    refs = [_text_ref(record.get(key)) for key in ("artifact_ref", "output_path", "path")]
    params = record.get("parameters")
    if isinstance(params, dict):
        refs.extend(_text_ref(params.get(key)) for key in ("path", "target_path", "output_path", "artifact_ref"))
    refs.extend(_record_ref_items(record.get("tool_result_refs"), ("path",)))
    refs.extend(_record_ref_items(record.get("artifact_registry_refs"), ("path",)))
    return [ref for ref in refs if ref]


def _record_ref_items(value: object, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_first_record_ref(item, keys) for item in value if isinstance(item, dict)]


def _first_record_ref(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _text_ref(item.get(key))
        if text:
            return text
    return ""


def _is_task_output_report(path: Path, output_dir: Path) -> bool:
    try:
        path.relative_to(output_dir)
    except ValueError:
        return False
    if not path.is_file():
        return False
    if path.suffix.lower() not in {".md", ".txt", ".json", ".html", ".csv", ".xlsx", ".docx", ".pptx"}:
        return False
    name = path.name.lower()
    return any(marker in name for marker in ("report", "analysis", "summary", "final", "结果", "报告", "分析", "总结"))


def _unique_artifact_payloads(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for artifact in artifacts:
        key = str(artifact.get("path") or "")
        if key and key not in seen:
            seen.add(key)
            result.append(artifact)
    return result


def _uncontracted_closeout_text(report: dict[str, Any]) -> str:
    payload = {
        "ok": True,
        "case_id": "",
        "report_ref": report.get("report_ref", ""),
        "delivery_mode": report.get("delivery_mode", ""),
        "artifacts": [_closeout_artifact_payload(item) for item in report["artifacts"]],
    }
    return (
        "[MAIN_AGENT_DELIVERY_COMPLETE]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n[/MAIN_AGENT_DELIVERY_COMPLETE]\n"
        "交付验收通过。本轮已写入 task output 下的报告类交付物，主代理停止继续工具循环。"
    )


def _closeout_artifact_payload(item: dict[str, Any]) -> dict[str, object]:
    return {
        "artifact_id": item["artifact_id"],
        "kind": item["kind"],
        "path": item["path"],
        "ok": item["ok"],
    }


def _text_ref(value: object) -> str:
    return str(value or "").strip()


__all__ = ["uncontracted_task_output_closeout_response"]
