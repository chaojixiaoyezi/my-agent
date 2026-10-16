# LLM: 上下文合同投影同一 run 的冻结事实；工具清单存为 owner 内容寻址引用，不能产生第二执行权威。
# 模块用途: 构造上下文包的范围、工具、产物与自检字段，支持新引用及旧快照读取。
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..common.value_parsing import text_value as _text
from ..contracts.tool_manifest_contract import tool_manifest_payload
from .context_bundle_tool_manifest import build_tool_manifest_summary

# 主上下文捆的最大提示段 1600 字符：够放身份/范围/契约摘要，又不至于挤占实际对话预算。
MAIN_CONTEXT_BUNDLE_PROMPT_MAX_CHARS = 1600
MAIN_CONTEXT_BUNDLE_REQUIRED_FIELDS = [
    "identity",
    "scope",
    "run_scope",
    "tool_manifest",
    "acceptance_contract",
    "self_check",
]
# LLM: request.save 是快照与工具清单共同的落盘控制；不保存时所有合同构造必须无写入。
# 函数用途: 从原运行事实构造上下文合同段，保存时复用 owner 工具清单。
def main_context_contract_sections(request: Any, home_paths: Any | None) -> dict[str, Any]:
    return {
        "schema_policy": _schema_policy(),
        "owner_model": _owner_model(request, home_paths),
        "run_scope": _run_scope(request),
        "tool_manifest": _tool_manifest(request, home_paths),
        "artifact_refs": _artifact_refs(request),
        "acceptance_contract": _acceptance_contract(request),
        "prompt_budget": _prompt_budget(0),
    }


def finalize_context_bundle_contracts(bundle: dict[str, Any], prompt_section: str) -> dict[str, Any]:
    result = dict(bundle)
    result["prompt_budget"] = _prompt_budget(len(prompt_section))
    result["self_check"] = _self_check(result)
    return result


def _schema_policy() -> dict[str, Any]:
    return {
        "schema": "main_context_bundle.v1",
        "version": 1,
        "required_fields": list(MAIN_CONTEXT_BUNDLE_REQUIRED_FIELDS),
        "required_reader_policy": "missing_required_field_sets_self_check_false_but_reader_must_degrade",
        "optional_reader_policy": "missing_optional_field_defaults_to_empty",
        "future_fields": "additive_only_until_v2",
    }


def _owner_model(request: Any, home_paths: Any | None) -> dict[str, Any]:
    run_id = _text(getattr(request, "run_id", ""))
    return {
        "owner_type": _text(getattr(request, "owner_type", "")) or "main_agent",
        "owner_id": _text(getattr(request, "owner_id", "")) or "root",
        "root_run_id": _text(getattr(request, "root_run_id", "")) or run_id,
        "parent_run_id": _text(getattr(request, "parent_run_id", "")),
        "task_workspace_refs": _task_workspace_refs(home_paths),
        "memory_scope": "main_agent_home",
    }


def _run_scope(request: Any) -> dict[str, Any]:
    root = Path(getattr(request, "root", "")).expanduser().resolve()
    workspace_roots = _paths(getattr(request, "workspace_roots", ()) or (str(root),), base=root)
    write_boundary = getattr(request, "write_boundary", None) if isinstance(getattr(request, "write_boundary", None), dict) else {}
    allowed = _paths(write_boundary.get("allowed_write_roots") or workspace_roots or [root], base=root)
    forbidden = _paths(write_boundary.get("forbidden_write_roots"), base=root)
    locked = _paths(write_boundary.get("locked_files"), base=root)
    return {
        "primary_workspace_root": str(root),
        "workspace_roots": [str(item) for item in workspace_roots],
        "allowed_write_roots": [str(item) for item in allowed],
        "forbidden_write_roots": [str(item) for item in forbidden],
        "locked_files": [str(item) for item in locked],
        "path_style": "windows" if os.name == "nt" else "posix",
        "permission_mode": "workspace_scoped",
    }


# LLM: 快照只存摘要（小字段 + manifest_sha256 + manifest_ref）；只有保存快照（request.save）时才把
#   完整清单按内容哈希写入本用户目录，同一工具集跨请求复用同一个文件，不保存则没有落盘副作用。
#   需要完整清单的读者走 context_bundle_tool_manifest.load_bundle_tool_manifest（传入快照文件自己的路径）。
# 函数用途: 生成快照的 tool_manifest 段：投影本 run 的不可变工具快照，并按需把完整清单落到本用户目录。
def _tool_manifest(request: Any, home_paths: Any | None) -> dict[str, Any]:
    from ..tooling.models import ToolRuntimeSnapshot

    snapshot = getattr(request, "tool_runtime_snapshot", None)
    if not isinstance(snapshot, ToolRuntimeSnapshot):
        snapshot = ToolRuntimeSnapshot(
            run_id=_text(getattr(request, "run_id", "")),
            runtimes=(),
            available_tool_names=frozenset(),
            unavailable_tools=(),
            allowed_tools=None,
            owner_type=_owner_type(request),
        )
    payload = tool_manifest_payload(snapshot)
    payload["tool_load_errors"] = [
        dict(item)
        for item in _sequence(getattr(request, "tool_runtime_errors", ()))
        if isinstance(item, dict)
    ]
    return build_tool_manifest_summary(
        payload, home_paths, save=bool(getattr(request, "save", True))
    )


def _artifact_refs(request: Any) -> dict[str, Any]:
    items = [
        {"ref": ref, "kind": "artifact_ref", "source": "context_bundle_request"}
        for ref in _context_texts(getattr(request, "artifact_refs", ()))
    ]
    return {
        "items": items,
        "collection_phase": "pre_tool_loop",
        "body_policy": "refs_only_read_explicitly",
    }


def _acceptance_contract(request: Any) -> dict[str, Any]:
    attrs = getattr(request, "task_attributes", None) if isinstance(getattr(request, "task_attributes", None), dict) else {}
    items = _context_texts(attrs.get("acceptance"))
    constraints = _context_texts(attrs.get("constraints"))
    latest_tests = _context_texts(attrs.get("latest_tests"))
    return {
        "source_status": "explicit_from_task_attributes" if items or constraints or latest_tests else "not_recorded",
        "items": items,
        "constraints": constraints,
        "latest_tests": latest_tests,
    }


def _self_check(bundle: dict[str, Any]) -> dict[str, Any]:
    checks = [
        *_required_field_checks(bundle),
        *_path_checks(bundle),
        _prompt_budget_check(bundle),
    ]
    return {
        "ok": all(item["ok"] for item in checks if item["severity"] == "hard"),
        "checks": checks,
    }


def _required_field_checks(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": f"required_{field}",
            "ok": isinstance(bundle.get(field), dict) and bool(bundle.get(field)),
            "severity": "hard",
        }
        for field in MAIN_CONTEXT_BUNDLE_REQUIRED_FIELDS
        if field != "self_check"
    ]


def _path_checks(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    workspace = _dict(bundle.get("workspace_refs"))
    recovery = _dict(bundle.get("recovery_refs"))
    return [
        _exists_check("workspace_root_exists", workspace.get("primary_workspace_root"), "hard"),
        _exists_check("my_agent_home_exists", workspace.get("my_agent_home"), "soft"),
        _exists_check("owner_memory_root_exists", workspace.get("owner_memory_root"), "soft"),
        _exists_check("compact_applies_root_exists", recovery.get("compact_applies_root"), "soft"),
    ]


def _prompt_budget_check(bundle: dict[str, Any]) -> dict[str, Any]:
    budget = _dict(bundle.get("prompt_budget"))
    actual = int(budget.get("prompt_section_chars", 0) or 0)
    maximum = int(budget.get("max_prompt_section_chars", MAIN_CONTEXT_BUNDLE_PROMPT_MAX_CHARS) or 0)
    return {"name": "prompt_section_within_budget", "ok": actual <= maximum, "severity": "hard"}


def _prompt_budget(chars: int) -> dict[str, Any]:
    return {
        "max_prompt_section_chars": MAIN_CONTEXT_BUNDLE_PROMPT_MAX_CHARS,
        "prompt_section_chars": int(chars),
        "full_json_policy": "persist_to_file_not_prompt",
    }


def _task_workspace_refs(home_paths: Any | None) -> dict[str, str]:
    if home_paths is None:
        return {}
    owner_tasks_dir = getattr(home_paths, "owner_tasks_dir", None)
    if owner_tasks_dir is None:
        return {}
    owner_tasks = Path(owner_tasks_dir).resolve()
    return {
        "owner_tasks_root": str(owner_tasks),
    }


def _exists_check(name: str, value: object, severity: str) -> dict[str, Any]:
    path = _text(value)
    return {"name": name, "ok": bool(path and Path(path).exists()), "severity": severity, "path": path}


def _paths(value: object, *, base: Path) -> list[Path]:
    result: list[Path] = []
    for item in _sequence(value):
        raw = _text(item)
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        resolved = path.resolve(strict=False)
        if resolved not in result:
            result.append(resolved)
    return result


def _context_texts(value: object) -> list[str]:
    return [_text(item) for item in _sequence(value) if _text(item)]


def _sequence(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return list(value)
    return [value]


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}

def _owner_type(request: Any) -> str:
    return _text(getattr(request, "owner_type", "")) or "main_agent"


__all__ = ["finalize_context_bundle_contracts", "main_context_contract_sections"]
