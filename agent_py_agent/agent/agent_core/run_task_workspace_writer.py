
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from ..common.json_io import append_jsonl_records, write_json_file_atomic
from ..user_space.run_workspace import EnsureRunWorkspaceRequest, ensure_run_workspace
from ._runtime_params import ArchiveRunParams
from .run_task_workspace_index import register_saved_run_task_ref


def current_run_task_workspace_root(agent, params: object | None = None) -> Path | None:
    for text in _task_workspace_root_candidates(agent, params):
        if not text:
            continue
        try:
            return Path(text).expanduser().resolve(strict=False)
        except OSError:
            continue
    return None


def write_run_task_workspace_if_needed(agent, params: ArchiveRunParams) -> str:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return ""
    home_paths = getattr(agent, "home_paths", None)
    if home_paths is None:
        return ""
    existing = _existing_workspace_paths(getattr(params, "task_attributes", None))
    if existing is not None:
        existing.root.mkdir(parents=True, exist_ok=True)
        existing.output_dir.mkdir(parents=True, exist_ok=True)
        existing.work_dir.mkdir(parents=True, exist_ok=True)
        register_saved_run_task_ref(agent, _SavedWorkspaceRef(existing.root, existing.work_dir), _root_task_params(params))
        return str(existing.root)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    target_home = Path(owner_home) if owner_home else Path(home_paths.root)
    result = ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=target_home,
            template=str(getattr(agent.config, "workspace_task_path_template", "")),
            task_name=params.task_id or params.user_prompt,
            user_prompt=params.user_prompt,
            request_id=params.run_request_id,
            run_id=params.run_id,
            task_id=params.task_id,
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            owner_home=str(getattr(home_paths, "owner_home_dir", "") or ""),
            source=params.source,
        )
    )
    register_saved_run_task_ref(agent, result, params)
    return str(result.root)


def sync_run_task_workspace_closeout(agent, params: object, report: dict[str, Any]) -> str:
    root = current_run_task_workspace_root(agent, params)
    if root is None:
        return ""
    work_dir = root / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    now = _now_iso()
    _write_closeout_state(work_dir / "state.json", params, report, now)
    _write_closeout_manifest(work_dir / "refs" / "artifacts" / "manifest.json", params, report, now)
    _append_closeout_timeline(work_dir / "timeline.jsonl", params, report, now)
    return str(root)


def attach_run_task_workspace_context(agent, params, user_prompt: str):
    if not _should_create_workspace(agent, params):
        return params
    result = _ensure_workspace_for_run(agent, params, user_prompt)
    injection = _workspace_prompt_section(result)
    next_inject = _append_once(list(getattr(params, "inject", None) or []), injection)
    next_attrs = _task_attributes_with_workspace(getattr(params, "task_attributes", None), result)
    next_contract = _delivery_contract_with_workspace(getattr(params, "delivery_contract", None), result)
    agent._current_run_task_workspace = str(result.root)
    return replace(params, inject=next_inject, task_attributes=next_attrs, delivery_contract=next_contract)


def _should_create_workspace(agent, params) -> bool:
    if not bool(getattr(agent.config, "run_task_workspace_enabled", True)):
        return False
    if str(getattr(params, "context_scope", "") or "").strip().lower() in {"task_local", "control_plane"}:
        return False
    return bool(getattr(agent, "home_paths", None) is not None)


def _ensure_workspace_for_run(agent, params, user_prompt: str):
    existing = _existing_workspace_paths(getattr(params, "task_attributes", None))
    if existing is not None:
        existing.root.mkdir(parents=True, exist_ok=True)
        existing.output_dir.mkdir(parents=True, exist_ok=True)
        existing.work_dir.mkdir(parents=True, exist_ok=True)
        return existing
    home_paths = agent.home_paths
    owner_home = getattr(home_paths, "owner_home_dir", None)
    target_home = Path(owner_home) if owner_home else Path(home_paths.root)
    return ensure_run_workspace(
        EnsureRunWorkspaceRequest(
            home=target_home,
            template=str(getattr(agent.config, "workspace_task_path_template", "")),
            task_name=getattr(params, "task_id", "") or user_prompt,
            user_prompt=user_prompt,
            request_id=str(getattr(params, "request_id", "") or ""),
            run_id=str(getattr(params, "run_id", "") or ""),
            task_id=str(getattr(params, "task_id", "") or ""),
            owner_id=str(getattr(home_paths, "owner_id", "") or ""),
            owner_home=str(getattr(home_paths, "owner_home_dir", "") or ""),
            source=str(getattr(params, "source", "") or "run"),
        )
    )


@dataclass(frozen=True)
class _ExistingWorkspacePaths:
    root: Path
    output_dir: Path
    work_dir: Path


@dataclass(frozen=True)
class _SavedWorkspaceRef:
    root: Path
    work_dir: Path


def _root_task_params(params: ArchiveRunParams) -> ArchiveRunParams:
    root_task_id = _conversation_task_id(getattr(params, "task_attributes", None))
    if not root_task_id:
        return params
    return replace(params, task_id=root_task_id)


def _conversation_task_id(attrs: object) -> str:
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("conversation_task_id") or "").strip()


def _existing_workspace_paths(attrs: object) -> _ExistingWorkspacePaths | None:
    if not isinstance(attrs, dict):
        return None
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    root_text = str(workspace.get("task_root") or "").strip()
    if not root_text:
        return None
    root = Path(root_text)
    output_dir = Path(str(workspace.get("output_dir") or root / "output"))
    work_dir = Path(str(workspace.get("work_dir") or root / "work"))
    return _ExistingWorkspacePaths(root=root, output_dir=output_dir, work_dir=work_dir)


def _task_workspace_root_candidates(agent, params: object | None) -> list[str]:
    attrs = getattr(params, "task_attributes", None) if params is not None else None
    contract = getattr(params, "delivery_contract", None) if params is not None else None
    return [
        _workspace_root_from_mapping(contract, "task_workspace"),
        _workspace_root_from_mapping(attrs, "run_workspace"),
        str(getattr(agent, "_current_run_task_workspace", "") or "").strip(),
    ]


def _workspace_root_from_mapping(value: object, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    workspace = value.get(key)
    if not isinstance(workspace, dict):
        return ""
    root = str(workspace.get("task_root") or "").strip()
    if root:
        return root
    for field in ("output_dir", "work_dir"):
        text = str(workspace.get(field) or "").strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser()
        except OSError:
            continue
        if field == "output_dir":
            return str(path.parent)
        if field == "work_dir":
            return str(path.parent)
    return ""


def _write_closeout_state(path: Path, params: object, report: dict[str, Any], now: str) -> None:
    state = _read_json(path)
    artifacts = _closeout_artifacts(report)
    artifact_refs = _unique_strings(
        [*list(state.get("artifact_refs", []) or []), *[str(item.get("path") or "") for item in artifacts]]
    )
    evidence_refs = _unique_strings([*list(state.get("evidence_refs", []) or []), str(report.get("report_ref") or "")])
    state.update(
        {
            "version": int(state.get("version") or 1),
            "task_id": str(state.get("task_id") or getattr(params, "task_id", "") or getattr(params, "run_id", "") or ""),
            "primary_run_id": str(
                state.get("primary_run_id") or getattr(params, "run_id", "") or getattr(params, "request_id", "") or ""
            ),
            "status": "DONE" if bool(report.get("ok")) else "FAILED",
            "verification_status": "VERIFIED" if bool(report.get("ok")) else "FAILED",
            "progress": 1.0 if bool(report.get("ok")) else float(state.get("progress") or 0.0),
            "latest_summary": _closeout_summary(report),
            "artifact_refs": artifact_refs,
            "evidence_refs": evidence_refs,
            "delivery_closeout": {
                "ok": bool(report.get("ok")),
                "report_ref": str(report.get("report_ref") or ""),
                "artifact_count": len(artifacts),
                "run_id": str(getattr(params, "run_id", "") or ""),
                "request_id": str(getattr(params, "request_id", "") or ""),
            },
            "updated_at": now,
        }
    )
    write_json_file_atomic(path, state, sort_keys=False)


def _write_closeout_manifest(path: Path, params: object, report: dict[str, Any], now: str) -> None:
    manifest = _read_json(path)
    manifest.update(
        {
            "version": int(manifest.get("version") or 1),
            "request_id": str(manifest.get("request_id") or getattr(params, "request_id", "") or ""),
            "run_id": str(manifest.get("run_id") or getattr(params, "run_id", "") or ""),
            "task_id": str(manifest.get("task_id") or getattr(params, "task_id", "") or ""),
            "artifacts": [_manifest_artifact(item) for item in _closeout_artifacts(report)],
            "closeout_report_ref": str(report.get("report_ref") or ""),
            "updated_at": now,
        }
    )
    write_json_file_atomic(path, manifest, sort_keys=False)


def _append_closeout_timeline(path: Path, params: object, report: dict[str, Any], now: str) -> None:
    append_jsonl_records(
        path,
        [
            {
                "event_type": "delivery_closeout_synced",
                "request_id": str(getattr(params, "request_id", "") or ""),
                "run_id": str(getattr(params, "run_id", "") or ""),
                "task_id": str(getattr(params, "task_id", "") or ""),
                "status": "DONE" if bool(report.get("ok")) else "FAILED",
                "verification_status": "VERIFIED" if bool(report.get("ok")) else "FAILED",
                "closeout_report_ref": str(report.get("report_ref") or ""),
                "artifact_count": len(_closeout_artifacts(report)),
                "created_at": now,
            }
        ],
        sort_keys=True,
    )


def _closeout_artifacts(report: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = report.get("artifacts")
    return [item for item in artifacts if isinstance(item, dict)] if isinstance(artifacts, list) else []


def _manifest_artifact(item: dict[str, Any]) -> dict[str, Any]:
    registry_ref = item.get("registry_ref") if isinstance(item.get("registry_ref"), dict) else {}
    acceptance = item.get("acceptance_report") if isinstance(item.get("acceptance_report"), dict) else {}
    return {
        "artifact_id": str(item.get("artifact_id") or registry_ref.get("artifact_id") or ""),
        "kind": str(item.get("kind") or registry_ref.get("kind") or ""),
        "path": str(item.get("path") or registry_ref.get("path") or ""),
        "ok": bool(item.get("ok")),
        "registry_ref": registry_ref,
        "acceptance_ok": bool(acceptance.get("ok", item.get("ok"))),
        "finding_codes": _finding_codes(acceptance),
    }


def _finding_codes(report: dict[str, Any]) -> list[str]:
    findings = report.get("findings")
    if not isinstance(findings, list):
        return []
    return [str(item.get("code") or "") for item in findings if isinstance(item, dict) and item.get("code")]


def _closeout_summary(report: dict[str, Any]) -> str:
    if bool(report.get("ok")):
        count = len(_closeout_artifacts(report))
        return f"交付验收通过，已登记 {count} 个最终产物。"
    return "交付验收未通过，已记录 closeout 报告。"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _unique_strings(values: list[object]) -> list[str]:
    return list(dict.fromkeys(text for value in values if (text := str(value or "").strip())))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workspace_prompt_section(paths) -> str:
    return "\n".join(
        [
            "# Current Task Workspace",
            "- 本轮任务已有独立任务目录；没有用户明确指定其他输出目录时，最终交付物写到 output_dir。",
            "- output_dir 可以作为协作时的共享产物区；代码、报告分片、子代理阶段产物可以先放这里方便联调和汇总。",
            "- 收口前请整理 output_dir：最终只保留用户需要看的交付物；明显的草稿、日志、子代理分报告和临时材料挪到 work_dir 或在最终报告里做索引。",
            "- work_dir 用于草稿、日志、中间材料和过程文件，也适合保存被挪走的过程产物。",
            "- 用户让你阅读、分析、扫描的项目/源码/资料目录是输入目录，不是默认交付目录。",
            "- 不要因为输入目录下面可以新建 output/，就把它当成本轮输出目录。",
            "- 输入目录里的 output/、reports/ 或旧报告只能当线索；除非用户明确要求复用，不能当成本轮已完成证据。",
            "- 即使读到旧报告，也要重新读取当前源码或文件，并把本轮产物登记/写入本轮 output_dir 或 work_dir。",
            "- 如果用户只要求聊天回答、不需要文件，可以正常直接回答，不必强行落盘。",
            f"- task_root: {paths.root}",
            f"- output_dir: {paths.output_dir}",
            f"- work_dir: {paths.work_dir}",
        ]
    )


def _append_once(items: list[str], injection: str) -> list[str]:
    marker = "# Current Task Workspace"
    return items if any(marker in str(item) for item in items) else [*items, injection]


def _task_attributes_with_workspace(attrs: object, paths) -> dict:
    result = dict(attrs) if isinstance(attrs, dict) else {}
    result["run_workspace"] = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    return result


def _delivery_contract_with_workspace(contract: object, paths):
    if not isinstance(contract, dict):
        return contract
    result = dict(contract)
    task_workspace = {
        "task_root": str(paths.root),
        "output_dir": str(paths.output_dir),
        "work_dir": str(paths.work_dir),
    }
    artifacts = result.get("artifacts")
    user_requested_output_dir = _user_requested_output_dir(artifacts, paths)
    if user_requested_output_dir:
        task_workspace["user_requested_output_dir"] = user_requested_output_dir
    result["task_workspace"] = task_workspace
    if isinstance(artifacts, list):
        result["artifacts"] = [_artifact_with_default_output_root(item, paths) for item in artifacts]
    return result


def _user_requested_output_dir(artifacts: object, paths) -> str:
    if not isinstance(artifacts, list):
        return ""
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        if directory := _user_requested_dir_from_artifact_path(item, paths):
            return directory
        if directory := _user_requested_dir_from_roots(item, paths):
            return directory
    return ""


def _user_requested_dir_from_artifact_path(artifact: dict, paths) -> str:
    text = str(artifact.get("preferred_path") or artifact.get("path") or "").strip()
    if not text or not _is_absolute_or_home_path(text):
        return ""
    try:
        path = Path(text).expanduser()
    except OSError:
        return ""
    if _same_or_inside(path, Path(paths.output_dir)):
        return ""
    return _output_dir_for_path_text(text)


def _user_requested_dir_from_roots(artifact: dict, paths) -> str:
    roots = artifact.get("allowed_output_roots")
    if not isinstance(roots, list):
        return ""
    for value in roots:
        text = str(value or "").strip()
        if not text or not _is_absolute_or_home_path(text):
            continue
        try:
            path = Path(text).expanduser()
        except OSError:
            continue
        if not _same_or_inside(path, Path(paths.output_dir)):
            return _output_dir_for_path_text(text)
    return ""


def _same_or_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _artifact_with_default_output_root(item: object, paths) -> object:
    if not isinstance(item, dict):
        return item
    artifact = dict(item)
    artifact = _artifact_with_task_output_paths(artifact, paths)
    if _artifact_declares_output_target(artifact):
        return artifact
    artifact["allowed_output_roots"] = [str(paths.output_dir)]
    return artifact


def _artifact_with_task_output_paths(artifact: dict, paths) -> dict:
    for key in ("preferred_path", "path"):
        rewritten = _rewrite_task_output_path(artifact.get(key), paths)
        if rewritten:
            artifact[key] = rewritten
    roots = artifact.get("allowed_output_roots")
    if isinstance(roots, list):
        artifact["allowed_output_roots"] = [
            _rewrite_task_output_root(value, paths) or value for value in roots
        ]
    if not _has_allowed_output_roots(artifact):
        explicit_root = _explicit_output_root_from_artifact_path(artifact, paths)
        if explicit_root:
            artifact["allowed_output_roots"] = [explicit_root]
    return artifact


def _rewrite_task_output_path(value: object, paths) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _is_absolute_or_home_path(text):
        return text
    suffix = _relative_output_suffix(text)
    if suffix is None:
        return text
    return str((paths.output_dir / suffix).resolve(strict=False)) if suffix else str(paths.output_dir)


def _rewrite_task_output_root(value: object, paths) -> str:
    text = str(value or "").strip()
    if not text or _is_absolute_or_home_path(text):
        return text
    suffix = _relative_output_suffix(text)
    if suffix is None:
        return text
    return str((paths.output_dir / suffix).resolve(strict=False)) if suffix else str(paths.output_dir)


def _relative_output_suffix(text: str) -> Path | None:
    normalized = text.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized == "output":
        return Path()
    if normalized.startswith("output/"):
        return Path(normalized[len("output/") :])
    return None


def _is_absolute_or_home_path(text: str) -> bool:
    return text.startswith("/") or text.startswith("~") or _is_windows_absolute_path(text)


def _has_allowed_output_roots(artifact: dict) -> bool:
    roots = artifact.get("allowed_output_roots")
    return isinstance(roots, list) and any(str(item or "").strip() for item in roots)


def _explicit_output_root_from_artifact_path(artifact: dict, paths) -> str:
    text = str(artifact.get("preferred_path") or artifact.get("path") or "").strip()
    if not text:
        return ""
    if not _is_absolute_or_home_path(text):
        return ""
    try:
        path = Path(text).expanduser()
    except OSError:
        return ""
    if _same_or_inside(path, Path(paths.output_dir)):
        return ""
    return _output_dir_for_path_text(text)


def _output_dir_for_path_text(text: str) -> str:
    pure = _pure_path(text)
    target = pure.parent if pure.suffix else pure
    result = str(target)
    return result if result != "." else "."


def _pure_path(text: str):
    return PureWindowsPath(text) if _is_windows_path(text) else Path(text).expanduser()


def _is_windows_path(text: str) -> bool:
    return _is_windows_absolute_path(text) or "\\" in text


def _is_windows_absolute_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or re.match(r"^\\\\[^\\/]+[\\/][^\\/]+", text))


def _artifact_declares_output_target(artifact: dict) -> bool:
    if str(artifact.get("preferred_path") or artifact.get("path") or "").strip():
        return True
    for key in ("allowed_output_roots", "search_roots", "artifact_roots"):
        value = artifact.get(key)
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            return True
    return False


__all__ = [
    "attach_run_task_workspace_context",
    "current_run_task_workspace_root",
    "sync_run_task_workspace_closeout",
    "write_run_task_workspace_if_needed",
]
