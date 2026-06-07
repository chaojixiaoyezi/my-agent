
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._runtime_params import ToolLoopExecuteParams
from .delivery_closeout.uncontracted import _absolute_paths_in_text
from .target_coverage_ledger import collect_target_coverage_records, target_coverage_status

_HINT_MARKER = "[delivery-completion-soft-hint]"
_MUTATING_TOOLS = {"write_file", "apply_patch", "run_command", "controlled_exec"}


def maybe_append_delivery_completion_soft_hint(
    agent: object,
    params: ToolLoopExecuteParams,
    archive_record: dict[str, object],
    *,
    tool_ok: bool,
) -> None:
    if _hint_already_added(params):
        return
    contract = params.delivery_contract if isinstance(params.delivery_contract, dict) else {}
    workspace_root = Path(getattr(agent, "root", ".")).expanduser().resolve(strict=False)
    target_paths = _required_target_paths(contract, workspace_root)
    completion_signal = _is_successful_delivery_signal(archive_record, tool_ok=tool_ok, target_paths=target_paths)
    if _target_coverage_blocks_auto_closeout(params, workspace_root):
        return
    if not completion_signal and not _coverage_completion_signal(
        params,
        tool_ok=tool_ok,
        target_paths=target_paths,
        workspace_root=workspace_root,
    ):
        return
    if not contract and not _is_task_output_delivery(params, archive_record):
        return
    ready_targets = [str(path) for path in target_paths if path.exists()]
    if target_paths and len(ready_targets) < len(target_paths):
        return
    produced_refs = _produced_refs(archive_record)
    if not target_paths and not produced_refs:
        return
    payload = {
        "status": "delivery_artifacts_present",
        "ready_target_paths": ready_targets,
        "produced_refs": produced_refs,
        "next_step_hint": "final_check_then_submit_or_report",
    }
    params.tool_context.append(
        "\n".join(
            [
                _HINT_MARKER,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "如果这些已经是本次任务要交付的最终产物，可以做一次必要的查漏补缺；"
                "确认无明显遗漏后请尽快调用 submit_for_acceptance 提交验收，"
                "或在无需落盘验收的任务里直接用简短中文汇报产物位置和完成情况。"
                "不要因为想再看看就无限重复读取同一批材料。",
            ]
        )
    )


def _is_successful_delivery_signal(
    record: dict[str, object],
    *,
    tool_ok: bool,
    target_paths: list[Path],
) -> bool:
    if _is_successful_mutation(record, tool_ok=tool_ok):
        return True
    return _is_successful_target_artifact_read(record, tool_ok=tool_ok, target_paths=target_paths)


def _is_successful_mutation(record: dict[str, object], *, tool_ok: bool) -> bool:
    if not tool_ok:
        return False
    tool = str(record.get("tool") or "").strip()
    if tool not in _MUTATING_TOOLS:
        return False
    return True


def _is_successful_target_artifact_read(
    record: dict[str, object],
    *,
    tool_ok: bool,
    target_paths: list[Path],
) -> bool:
    if not tool_ok or not target_paths:
        return False
    tool = str(record.get("tool") or "").strip()
    if tool not in {"read_file", "read_artifact"}:
        return False
    refs = [_path_from_ref(ref) for ref in [*_produced_refs(record), *_record_path_refs(record)]]
    refs = [path for path in refs if path is not None]
    return any(ref == target for ref in refs for target in target_paths)


def _required_target_paths(contract: dict[str, Any], workspace_root: Path) -> list[Path]:
    raw = contract.get("artifacts")
    if not isinstance(raw, list):
        return []
    paths: list[Path] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("required") is False:
            continue
        raw_path = str(item.get("preferred_path") or item.get("path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        paths.append(path.resolve(strict=False) if path.is_absolute() else (workspace_root / path).resolve(strict=False))
    return _unique_paths(paths)


def _produced_refs(record: dict[str, object]) -> list[str]:
    refs: list[str] = []
    for key in ("artifact_ref", "output_path", "path"):
        _append_text(refs, record.get(key))
    _append_ref_items(refs, record.get("artifact_registry_refs"), ("path", "artifact_id"))
    _append_ref_items(refs, record.get("tool_result_refs"), ("path",))
    return list(dict.fromkeys(refs))


def _record_path_refs(record: dict[str, object]) -> list[str]:
    refs: list[str] = []
    parameters = record.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    for key in ("path", "file_path", "target_path", "output_path", "artifact_ref", "source_ref"):
        _append_text(refs, parameters.get(key))
        _append_text(refs, record.get(key))
    _append_text(refs, record.get("source_input"))
    return list(dict.fromkeys(refs))


def _path_from_ref(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text or "://" in text:
        return None
    return Path(text).expanduser().resolve(strict=False)


def _is_task_output_delivery(params: ToolLoopExecuteParams, record: dict[str, object]) -> bool:
    targets = _accepted_output_targets(params)
    if not targets:
        return False
    for ref in _produced_refs(record):
        path = Path(ref).expanduser()
        if not path.is_absolute():
            continue
        resolved = path.resolve(strict=False)
        if _matches_any_target(resolved, targets) and _is_supported_delivery_file(resolved):
            return True
    return False


def _accepted_output_targets(params: ToolLoopExecuteParams) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    task_output = _task_output_dir(params)
    if task_output is not None:
        targets.append({"path": task_output, "kind": "dir"})
    targets.extend(_user_requested_output_targets(params))
    return _unique_targets(targets)


def _task_output_dir(params: ToolLoopExecuteParams) -> Path | None:
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return None
    workspace = attrs.get("run_workspace")
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("output_dir") or "").strip()
    if not text:
        return None
    return Path(text).expanduser().resolve(strict=False)


def _user_requested_output_targets(params: ToolLoopExecuteParams) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict):
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


def _output_target_for_user_path(path: Path, *, force_kind: str | None = None) -> dict[str, object]:
    resolved = path.resolve(strict=False)
    kind = force_kind or ("file" if resolved.suffix else "dir")
    return {"path": resolved, "kind": kind}


def _matches_any_target(path: Path, targets: list[dict[str, object]]) -> bool:
    for target in targets:
        root = target.get("path")
        if not isinstance(root, Path):
            continue
        if target.get("kind") == "file":
            if path == root:
                return True
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _is_supported_delivery_file(path: Path) -> bool:
    return path.suffix.lower() in {".md", ".txt", ".json", ".html", ".csv", ".xlsx", ".docx", ".pptx"}


def _hint_already_added(params: ToolLoopExecuteParams) -> bool:
    return any(str(item).startswith(_HINT_MARKER) for item in params.tool_context)


def target_coverage_blocks_delivery_auto_closeout(agent: object, params: ToolLoopExecuteParams) -> bool:
    workspace_root = Path(getattr(agent, "root", ".")).expanduser().resolve(strict=False)
    return _target_coverage_blocks_auto_closeout(params, workspace_root)


def _coverage_completion_signal(
    params: ToolLoopExecuteParams,
    *,
    tool_ok: bool,
    target_paths: list[Path],
    workspace_root: Path | None,
) -> bool:
    if not tool_ok or not target_paths:
        return False
    coverage = _target_coverage_contract(params)
    if not coverage:
        return False
    return _coverage_status_complete(_current_target_coverage_status(params, coverage, workspace_root))


def _target_coverage_blocks_auto_closeout(params: ToolLoopExecuteParams, workspace_root: Path | None) -> bool:
    coverage = _target_coverage_contract(params)
    if not coverage:
        return False
    return _coverage_status_blocks(_current_target_coverage_status(params, coverage, workspace_root))


def _target_coverage_contract(params: ToolLoopExecuteParams) -> dict[str, Any]:
    contract = params.delivery_contract if isinstance(params.delivery_contract, dict) else {}
    coverage = contract.get("target_coverage_contract")
    return dict(coverage) if isinstance(coverage, dict) else {}


def _current_target_coverage_status(
    params: ToolLoopExecuteParams,
    coverage_contract: dict[str, Any],
    workspace_root: Path | None,
) -> dict[str, object]:
    return target_coverage_status(
        coverage_contract,
        coverage_records=collect_target_coverage_records(
            list(getattr(params, "archive_tool_calls", []) or []),
            workspace_root=workspace_root,
        ),
        workspace_root=workspace_root,
    )


def _coverage_status_blocks(status: dict[str, object]) -> bool:
    return status.get("should_block") is True


def _coverage_status_complete(status: dict[str, object]) -> bool:
    return _positive_int(status.get("expected_count")) > 0 and _positive_int(status.get("missing_count")) == 0


def _positive_int(value: object) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _append_text(items: list[str], value: object) -> None:
    text = str(value or "").strip()
    if text:
        items.append(text)


def _append_ref_items(items: list[str], value: object, keys: tuple[str, ...]) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        _append_first_ref_value(items, item, keys)


def _append_first_ref_value(items: list[str], value: object, keys: tuple[str, ...]) -> None:
    if not isinstance(value, dict):
        return
    for key in keys:
        text = str(value.get(key) or "").strip()
        if text:
            items.append(text)
            return


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _unique_targets(targets: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, object]] = []
    for target in targets:
        path = target.get("path")
        kind = str(target.get("kind") or "")
        if not isinstance(path, Path) or kind not in {"file", "dir"}:
            continue
        key = (str(path), kind)
        if key in seen:
            continue
        seen.add(key)
        result.append(target)
    return result


__all__ = ["maybe_append_delivery_completion_soft_hint", "target_coverage_blocks_delivery_auto_closeout"]
