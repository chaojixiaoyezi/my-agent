from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ...artifacts.registry import ArtifactRegistration, register_artifact, registry_path
from ...backends import ModelResponse
from .._runtime_params import ToolLoopExecuteParams
from ..run_task_workspace_writer import sync_run_task_workspace_closeout
from ..tool_guard.local_progress import reset_local_progress_guard
from .artifacts import _relative_report_ref, _write_report
from .subagent_aggregation import evaluate_subagent_aggregation_gate

_PATH_TOKEN_RE = re.compile(
    r"(?P<path>"
    r"~[\\/][^\s'\"`<>()\[\]{}，。；;、]+"
    r"|(?<![:/])/(?!/)[^\s'\"`<>()\[\]{}，。；;、]+"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s'\"`<>()\[\]{}，。；;、]+"
    r"|\\\\[^\s'\"`<>()\[\]{}，。；;、]+"
    r")"
)


def uncontracted_task_output_closeout_response(
    request: object,
    workspace_root: Path,
) -> ModelResponse | None:
    artifacts = _current_run_task_output_artifacts(
        getattr(request, "params", None),
        workspace_root=_tool_workspace_root(getattr(request, "agent", None)),
    )
    if not artifacts:
        artifacts = _current_run_task_output_artifacts(getattr(request, "params", None), workspace_root=workspace_root)
    if not artifacts:
        return None
    params = request.params
    artifacts = _registered_artifacts(artifacts, workspace_root, params)
    delivery_mode = _delivery_mode_for_artifacts(artifacts)
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
        "delivery_mode": delivery_mode,
        "message_zh": _message_for_delivery_mode(delivery_mode),
    }
    report_ref = _write_report(workspace_root, report)
    report["report_ref"] = _relative_report_ref(report_ref, workspace_root)
    decision = evaluate_subagent_aggregation_gate(request)
    report["subagent_aggregation_gate"] = decision.to_dict()
    _write_report(workspace_root, report)
    sync_run_task_workspace_closeout(request.agent, params, report)
    reset_local_progress_guard(request.agent, params)
    return ModelResponse(text=_uncontracted_closeout_text(report), backend=request.backend)


def _current_run_task_output_artifacts(
    params: ToolLoopExecuteParams | None,
    *,
    workspace_root: Path | None = None,
) -> list[dict[str, Any]]:
    if params is None:
        return []
    targets = _accepted_output_targets(params)
    if not targets:
        return []
    artifacts: list[dict[str, Any]] = []
    for record in _successful_write_records(getattr(params, "archive_tool_calls", []) or []):
        artifacts.extend(_task_output_artifacts_from_record(record, targets, workspace_root=workspace_root))
    return _unique_artifact_payloads(artifacts)


def _registered_artifacts(
    artifacts: list[dict[str, Any]],
    workspace_root: Path,
    params: ToolLoopExecuteParams,
) -> list[dict[str, Any]]:
    registered: list[dict[str, Any]] = []
    for item in artifacts:
        record = register_artifact(
            ArtifactRegistration(
                workspace_root=workspace_root,
                path=str(item.get("path") or ""),
                artifact_id=str(item.get("artifact_id") or ""),
                run_id=str(getattr(params, "run_id", "") or ""),
                task_id=str(getattr(params, "task_id", "") or ""),
                agent_id=str(getattr(params, "run_id", "") or ""),
                kind=str(item.get("kind") or ""),
                source=str(item.get("source") or "current_run_tool_output"),
                created_by_tool="closeout",
                status="ready",
                metadata={"output_scope": str(item.get("output_scope") or "")},
            )
        )
        registered.append({**item, "registry_ref": record.to_dict()})
    return registered


def _tool_workspace_root(agent: object | None) -> Path | None:
    root = getattr(getattr(agent, "tools", None), "workspace_root", None) if agent is not None else None
    root = root or (getattr(agent, "root", None) if agent is not None else None)
    if not root:
        return None
    try:
        return Path(root).expanduser().resolve(strict=False)
    except OSError:
        return None


def _task_output_artifacts_from_record(
    record: dict[str, Any],
    targets: list[dict[str, Any]],
    *,
    workspace_root: Path | None = None,
) -> list[dict[str, Any]]:
    return [
        _artifact_payload(record, path, target)
        for path in _produced_paths(record, workspace_root=workspace_root)
        for target in targets
        if _is_task_output_file(path, target)
    ]


def _successful_write_records(records: object) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict) and _successful_write_record(record)]


def _artifact_payload(record: dict[str, Any], path: Path, target: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": str(record.get("call_id") or path.name),
        "kind": path.suffix.lower().lstrip(".") or "file",
        "path": str(path),
        "ok": True,
        "source": "current_run_tool_output",
        "output_scope": str(target["scope"]),
    }


def _task_output_dir(params: ToolLoopExecuteParams | None) -> Path | None:
    attrs = params.task_attributes if params is not None and isinstance(params.task_attributes, dict) else {}
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("output_dir") or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


def _accepted_output_targets(params: ToolLoopExecuteParams) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    task_output = _task_output_dir(params)
    if task_output is not None:
        targets.append({"path": task_output, "kind": "dir", "scope": "task_output"})
    targets.extend(_user_requested_output_targets(params))
    return _unique_targets(targets)


def _user_requested_output_targets(params: ToolLoopExecuteParams) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    workspace = attrs.get("run_workspace")
    if isinstance(workspace, dict):
        dir_text = str(workspace.get("user_requested_output_dir") or "").strip()
        if dir_text:
            targets.append(_output_target_for_user_path(Path(dir_text).expanduser(), force_kind="dir"))
        path_text = str(workspace.get("user_requested_output_path") or "").strip()
        if path_text:
            targets.append(_output_target_for_user_path(Path(path_text).expanduser()))
    prompt_text = "\n".join(
        text
        for text in (
            str(getattr(params, "root_user_prompt", "") or ""),
            str(getattr(params, "user_prompt", "") or ""),
        )
        if text
    )
    targets.extend(_output_target_for_user_path(path) for path in _absolute_paths_in_text(prompt_text))
    return _unique_targets(targets)


def _absolute_paths_in_text(text: str) -> list[Path]:
    if not text:
        return []
    paths: list[Path] = []
    for raw in _absolute_path_tokens_in_text(text):
        if _is_absolute_path_token(raw, platform_name=os.name):
            paths.append(Path(raw).expanduser())
    return paths


def _absolute_path_tokens_in_text(text: str) -> list[str]:
    source = str(text or "")
    tokens: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(source):
        if _is_inside_url_token(source, match.start()):
            continue
        raw = _clean_path_token(match.group("path"))
        if raw:
            tokens.append(raw)
    return tokens


def _is_inside_url_token(text: str, start: int) -> bool:
    prefix = text[:start]
    token_start = max(prefix.rfind(" "), prefix.rfind("\t"), prefix.rfind("\n")) + 1
    return "://" in prefix[token_start:]


def _is_absolute_path_token(token: str, *, platform_name: str) -> bool:
    text = str(token or "").strip()
    if not text:
        return False
    if text.startswith("~"):
        return True
    if platform_name == "nt":
        return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"))
    return text.startswith("/")


def _clean_path_token(value: str) -> str:
    return str(value or "").strip().rstrip(".,;，。；、")


def _output_target_for_user_path(path: Path, *, force_kind: str | None = None) -> dict[str, Any]:
    resolved = path.resolve(strict=False)
    kind = force_kind or ("file" if resolved.suffix else "dir")
    return {"path": resolved, "kind": kind, "scope": "user_requested_output"}


def _successful_write_record(record: dict[str, Any]) -> bool:
    if record.get("ok") is not True:
        return False
    return str(record.get("tool") or "").strip() in {"write_file", "apply_patch", "run_command", "controlled_exec"}


def _produced_paths(record: dict[str, Any], *, workspace_root: Path | None = None) -> list[Path]:
    refs = _record_refs(record)
    paths: list[Path] = []
    for ref in dict.fromkeys(refs):
        if "://" in ref:
            continue
        path = Path(ref).expanduser()
        if path.is_absolute():
            paths.append(path.resolve(strict=False))
        elif workspace_root is not None:
            paths.append((workspace_root / path).resolve(strict=False))
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


def _is_task_output_file(path: Path, target: dict[str, Any]) -> bool:
    output_root = target.get("path")
    if not isinstance(output_root, Path):
        return False
    if target.get("kind") == "file":
        if path != output_root:
            return False
    else:
        try:
            path.relative_to(output_root)
        except ValueError:
            return False
    if not path.is_file():
        return False
    return path.suffix.lower() in {".md", ".txt", ".json", ".html", ".csv", ".xlsx", ".docx", ".pptx"}


def _delivery_mode_for_artifacts(artifacts: list[dict[str, Any]]) -> str:
    scopes = {str(item.get("output_scope") or "") for item in artifacts}
    if "user_requested_output" in scopes:
        return "uncontracted_user_requested_output"
    return "uncontracted_task_output"


def _message_for_delivery_mode(delivery_mode: str) -> str:
    if delivery_mode == "uncontracted_user_requested_output":
        return "没有结构化交付合同，但本轮已写入用户明确指定路径下的报告类交付物，且通过当前 run 产物验收；主代理停止继续工具循环。"
    return "没有结构化交付合同，但本轮已写入 task output 下的报告类交付物，且通过当前 run 产物验收；主代理停止继续工具循环。"


def _unique_artifact_payloads(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for artifact in artifacts:
        key = str(artifact.get("path") or "")
        if key and key not in seen:
            seen.add(key)
            result.append(artifact)
    return result


def _unique_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for target in targets:
        path = target.get("path")
        if not isinstance(path, Path):
            continue
        kind = str(target.get("kind") or "")
        if kind not in {"file", "dir"}:
            continue
        key = (str(path), kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(target)
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
