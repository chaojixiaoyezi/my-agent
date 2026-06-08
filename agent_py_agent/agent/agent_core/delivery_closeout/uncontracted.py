from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ...artifacts.registry import ArtifactRegistration, register_artifact, registry_path
from ...backends import ModelResponse
from ...contracts.artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from .._runtime_params import ToolLoopExecuteParams
from ..run_task_workspace_writer import sync_run_task_workspace_closeout
from ..target_coverage_ledger import collect_target_coverage_records, target_coverage_status
from ..tool_guard.local_progress import reset_local_progress_guard
from .artifacts import _relative_report_ref, _write_report
from .evidence import target_coverage_projection_decision, target_coverage_projection_repair_message
from .recovery import attach_contract_recovery, failed_gate_payloads
from .subagent_aggregation import evaluate_subagent_aggregation_gate
from .task_progress_gate import evaluate_task_progress_closeout_gate, task_progress_repair_message

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
    artifact_blocks = any(item.get("ok") is not True for item in artifacts)
    coverage_status = _uncontracted_target_coverage_status(request, workspace_root)
    if coverage_status:
        report["target_coverage_status"] = coverage_status
    projection_decision = target_coverage_projection_decision(report)
    report["target_coverage_projection_gate"] = projection_decision.to_dict()
    task_progress_decision = evaluate_task_progress_closeout_gate(request, report)
    report["task_progress_closeout_gate"] = task_progress_decision.to_dict()
    decision = evaluate_subagent_aggregation_gate(request)
    report["subagent_aggregation_gate"] = decision.to_dict()
    decisions = [projection_decision, task_progress_decision, decision]
    coverage_blocks = coverage_status.get("should_block") is True if coverage_status else False
    if coverage_blocks:
        _attach_uncontracted_target_coverage_recovery(report)
    if artifact_blocks:
        _attach_uncontracted_artifact_recovery(report)
    if artifact_blocks or coverage_blocks or not all(item.allowed for item in decisions):
        report["ok"] = False
        attach_contract_recovery(report, decisions, contract={})
        _write_report(workspace_root, report)
        _append_uncontracted_repair_context(params, report)
        return None
    _write_report(workspace_root, report)
    sync_run_task_workspace_closeout(request.agent, params, report)
    reset_local_progress_guard(request.agent, params)
    return ModelResponse(text=_uncontracted_closeout_text(report), backend=request.backend)


def _append_uncontracted_repair_context(params: ToolLoopExecuteParams, report: dict[str, Any]) -> None:
    message = (
        task_progress_repair_message(report)
        or target_coverage_projection_repair_message(report)
        or _uncontracted_target_coverage_repair_message(report)
        or "当前交付物还没有通过结构化收口检查；请按 failed_gates 修复后重新提交。"
    )
    params.tool_context.append(
        "[delivery-closeout-check]\n"
        + json.dumps(
            {
                "ok": False,
                "report_ref": report.get("report_ref", ""),
                "failed_gates": failed_gate_payloads(report),
                "failed_artifacts": [
                    _closeout_artifact_payload(item)
                    for item in report.get("artifacts", [])
                    if isinstance(item, dict) and item.get("ok") is not True
                ],
                "target_coverage_status": report.get("target_coverage_status", {}),
                "repair_guidance": {
                    "mode": "closeout_rework",
                    "message_zh": message,
                    "submit_when_ready": "submit_for_acceptance",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _uncontracted_target_coverage_status(
    request: object,
    workspace_root: Path,
) -> dict[str, object]:
    params = getattr(request, "params", None)
    contract = _delivery_contract(params)
    coverage = contract.get("target_coverage_contract")
    if not isinstance(coverage, dict):
        return {}
    coverage_root = _tool_workspace_root(getattr(request, "agent", None)) or workspace_root
    return target_coverage_status(
        coverage,
        coverage_records=collect_target_coverage_records(
            list(getattr(params, "archive_tool_calls", []) or []),
            workspace_root=coverage_root,
        ),
        workspace_root=coverage_root,
    )


def _delivery_contract(params: ToolLoopExecuteParams | None) -> dict[str, Any]:
    if params is None:
        return {}
    value = params.delivery_contract
    if isinstance(value, dict):
        return dict(value)
    attrs = params.task_attributes if isinstance(params.task_attributes, dict) else {}
    value = attrs.get("delivery_contract")
    return dict(value) if isinstance(value, dict) else {}


def _attach_uncontracted_target_coverage_recovery(report: dict[str, Any]) -> None:
    recovery = report.setdefault("contract_recovery", {})
    if not isinstance(recovery, dict):
        recovery = {}
        report["contract_recovery"] = recovery
    actions = recovery.get("required_actions")
    if not isinstance(actions, list):
        actions = []
    actions.extend(
        action
        for action in (
            "cover_missing_targets_before_submit",
            "update_final_artifact_after_required_coverage",
            "submit_for_acceptance_after_coverage_is_complete",
        )
        if action not in actions
    )
    recovery["required_actions"] = actions


def _attach_uncontracted_artifact_recovery(report: dict[str, Any]) -> None:
    recovery = report.setdefault("contract_recovery", {})
    if not isinstance(recovery, dict):
        recovery = {}
        report["contract_recovery"] = recovery
    actions = recovery.get("required_actions")
    if not isinstance(actions, list):
        actions = []
    actions.extend(
        action
        for action in (
            "rewrite_partial_final_artifacts_with_complete_write",
            "submit_for_acceptance_after_final_artifacts_are_complete",
        )
        if action not in actions
    )
    recovery["required_actions"] = actions


def _uncontracted_target_coverage_repair_message(report: dict[str, Any]) -> str:
    status = report.get("target_coverage_status")
    if not isinstance(status, dict) or status.get("should_block") is not True:
        return ""
    hints = status.get("repair_hints") if isinstance(status.get("repair_hints"), list) else []
    hint_text = json.dumps(hints[:3], ensure_ascii=False, sort_keys=True)
    return (
        "当前验收失败是因为目录/来源覆盖清单还没完成。"
        "下一步按 target_coverage_status.repair_hints 的 recommended_tool_call 补读缺失目标；"
        "补完后更新最终交付物，再 submit_for_acceptance。"
        f" 当前可执行游标：{hint_text}"
    )


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
    archive_tool_calls = list(getattr(params, "archive_tool_calls", []) or [])
    for record in _successful_write_records(archive_tool_calls):
        artifacts.extend(
            _task_output_artifacts_from_record(
                record,
                targets,
                workspace_root=workspace_root,
                archive_tool_calls=archive_tool_calls,
            )
        )
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
                status="ready" if item.get("ok") is True else "invalid",
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
    archive_tool_calls: list[Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        _artifact_payload(record, path, target, archive_tool_calls=archive_tool_calls or [], workspace_root=workspace_root)
        for path in _produced_paths(record, workspace_root=workspace_root)
        for target in targets
        if _is_task_output_file(path, target)
    ]


def _successful_write_records(records: object) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict) and _successful_write_record(record)]


def _artifact_payload(
    record: dict[str, Any],
    path: Path,
    target: dict[str, Any],
    *,
    archive_tool_calls: list[Any],
    workspace_root: Path | None,
) -> dict[str, Any]:
    acceptance_report = _artifact_acceptance_report(path, target)
    findings = list(acceptance_report.get("findings") if isinstance(acceptance_report.get("findings"), list) else [])
    if finding := _partial_unclosed_artifact_finding(record, path):
        findings.append(finding)
    if finding := _unrecovered_unclosed_write_finding(record, path, archive_tool_calls, workspace_root):
        findings.append(finding)
    ok = bool(acceptance_report.get("ok")) and not any(str(item.get("severity") or "") == "hard" for item in findings)
    acceptance_report = {**acceptance_report, "ok": ok}
    if findings:
        acceptance_report["findings"] = findings
    return {
        "artifact_id": str(record.get("call_id") or path.name),
        "kind": path.suffix.lower().lstrip(".") or "file",
        "path": str(path),
        "ok": ok,
        "acceptance_report": acceptance_report,
        "source": "current_run_tool_output",
        "output_scope": str(target["scope"]),
    }


def _artifact_acceptance_report(path: Path, target: dict[str, Any]) -> dict[str, Any]:
    return validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=_validation_root_for_target(path, target),
            validation_contract={},
        )
    ).to_dict()


def _validation_root_for_target(path: Path, target: dict[str, Any]) -> Path:
    root = target.get("path")
    if isinstance(root, Path):
        return root if target.get("kind") == "dir" else root.parent
    return path.parent


def _partial_unclosed_artifact_finding(record: dict[str, Any], path: Path) -> dict[str, str]:
    params = record.get("parameters")
    if not isinstance(params, dict) or params.get("__partial_unclosed_write") is not True:
        return {}
    return {
        "code": "ARTIFACT_LAST_WRITE_PARTIAL_UNCLOSED",
        "severity": "hard",
        "message": "Final artifact path was last written by an incomplete partial write chunk.",
        "location": str(path),
        "value": str(record.get("call_id") or record.get("scoped_call_id") or ""),
        "action_zh": "最终交付物最后一次写入是半截分片；请用完整 write_file 覆盖或补成完整文件后再提交验收。",
    }


def _unrecovered_unclosed_write_finding(
    record: dict[str, Any],
    path: Path,
    archive_tool_calls: list[Any],
    workspace_root: Path | None,
) -> dict[str, str]:
    parse_errors = [
        item
        for item in archive_tool_calls
        if isinstance(item, dict)
        and str(item.get("tool") or "") == "__parse_error__"
        and str(item.get("error_code") or "") == "TOOL_CALL_UNCLOSED"
        and _record_targets_path(item, path, workspace_root)
    ]
    if len(parse_errors) < 2:
        return {}
    params = record.get("parameters")
    params = params if isinstance(params, dict) else {}
    if str(params.get("mode") or "overwrite") != "overwrite":
        return {}
    content = str(params.get("content") or "")
    max_chunk = _max_recovery_chunk_chars(parse_errors)
    if max_chunk <= 0 or len(content) > max_chunk * 2:
        return {}
    return {
        "code": "ARTIFACT_UNCLOSED_WRITE_RECOVERY_INCOMPLETE",
        "severity": "hard",
        "message": "Final artifact was accepted after repeated unclosed write_file attempts, but the latest overwrite is still only a small recovery chunk.",
        "location": str(path),
        "value": str(record.get("call_id") or record.get("scoped_call_id") or ""),
        "action_zh": "同一个最终文件多次长写入未闭合，最后只覆盖成一个小分片；请用 overwrite 写完整开头后，再用 mode=append 按 write_recovery.max_chunk_chars 分块续写，直到文件结构完整后再验收。",
    }


def _record_targets_path(record: dict[str, Any], path: Path, workspace_root: Path | None) -> bool:
    params = record.get("parameters")
    params = params if isinstance(params, dict) else {}
    raw = str(params.get("path") or "").strip()
    if not raw:
        recovery = params.get("write_recovery")
        if isinstance(recovery, dict):
            raw = str(recovery.get("path") or "").strip()
    if not raw:
        raw = str(record.get("source_input") or "").strip()
    if not raw or raw == "__parse_error__":
        return False
    return _canonical_path(Path(raw), workspace_root) == _canonical_path(path, workspace_root)


def _canonical_path(path: Path, workspace_root: Path | None) -> str:
    try:
        expanded = path.expanduser()
        if not expanded.is_absolute() and workspace_root is not None:
            expanded = workspace_root / expanded
        return str(expanded.resolve(strict=False))
    except OSError:
        return str(path)


def _max_recovery_chunk_chars(records: list[dict[str, Any]]) -> int:
    values: list[int] = []
    for record in records:
        params = record.get("parameters")
        recovery = params.get("write_recovery") if isinstance(params, dict) else None
        if isinstance(recovery, dict):
            try:
                values.append(int(recovery.get("max_chunk_chars") or 0))
            except (TypeError, ValueError):
                pass
    return max(values) if values else 0


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
    order: list[str] = []
    by_path: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        key = str(artifact.get("path") or "")
        if not key:
            continue
        if key not in by_path:
            order.append(key)
        by_path[key] = artifact
    return [by_path[key] for key in order]


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
