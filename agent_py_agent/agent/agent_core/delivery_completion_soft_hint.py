
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._runtime_params import ToolLoopExecuteParams

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
    if not _is_successful_mutation(archive_record, tool_ok=tool_ok):
        return
    contract = params.delivery_contract if isinstance(params.delivery_contract, dict) else {}
    if not contract:
        return
    workspace_root = Path(getattr(agent, "root", ".")).expanduser().resolve(strict=False)
    target_paths = _required_target_paths(contract, workspace_root)
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


def _is_successful_mutation(record: dict[str, object], *, tool_ok: bool) -> bool:
    if not tool_ok:
        return False
    tool = str(record.get("tool") or "").strip()
    if tool not in _MUTATING_TOOLS:
        return False
    return True


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
    for key in ("artifact_ref", "output_path"):
        _append_text(refs, record.get(key))
    _append_ref_items(refs, record.get("artifact_registry_refs"), ("path", "artifact_id"))
    _append_ref_items(refs, record.get("tool_result_refs"), ("path",))
    return list(dict.fromkeys(refs))


def _hint_already_added(params: ToolLoopExecuteParams) -> bool:
    return any(str(item).startswith(_HINT_MARKER) for item in params.tool_context)


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


__all__ = ["maybe_append_delivery_completion_soft_hint"]
