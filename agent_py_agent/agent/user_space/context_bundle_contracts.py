
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..common.value_parsing import text_value as _text
from ..contracts.tool_manifest_contract import tool_manifest_payload

MAIN_CONTEXT_BUNDLE_PROMPT_MAX_CHARS = 1600
MAIN_CONTEXT_BUNDLE_REQUIRED_FIELDS = [
    "identity",
    "scope",
    "run_scope",
    "tool_manifest",
    "acceptance_contract",
    "self_check",
]
def main_context_contract_sections(request: Any, home_paths: Any | None) -> dict[str, Any]:
    return {
        "schema_policy": _schema_policy(),
        "owner_model": _owner_model(request, home_paths),
        "run_scope": _run_scope(request),
        "tool_manifest": _tool_manifest(request),
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


def _tool_manifest(request: Any) -> dict[str, Any]:
    payload = tool_manifest_payload(
        list(_sequence(getattr(request, "tool_specs", ()))),
        allowed_tools=_context_texts(getattr(request, "allowed_tools", ())),
        granted_capabilities=_context_texts(getattr(request, "granted_capabilities", ())),
        owner_type=_owner_type(request),
    )
    payload["tool_specs"] = payload["tools"]
    payload["tool_load_errors"] = [
        dict(item)
        for item in _sequence(getattr(request, "tool_spec_errors", ()))
        if isinstance(item, dict)
    ]
    return payload


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
