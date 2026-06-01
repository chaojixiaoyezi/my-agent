# LLM: Main context bundle contract helpers keep schema/run/tool/acceptance surfaces explicit.
# 模块用途: 为主代理 context bundle 生成合同字段、自检和 prompt 预算，避免主文件继续膨胀。

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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
# LLM: main_context_contract_sections builds all non-memory contract sections in one predictable shape.
# 函数用途: 生成 schema 策略、owner model、run scope、tool manifest、artifact refs、验收合同和预算初值。
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


# LLM: finalize_context_bundle_contracts fills self-check and prompt budget after prompt rendering.
# 函数用途: 根据实际 prompt section 长度补齐预算和自检，保证写盘前 bundle 自带健康状态。
def finalize_context_bundle_contracts(bundle: dict[str, Any], prompt_section: str) -> dict[str, Any]:
    result = dict(bundle)
    result["prompt_budget"] = _prompt_budget(len(prompt_section))
    result["self_check"] = _self_check(result)
    return result


# LLM: _schema_policy is the migration contract for readers of main_context_bundle.v1.
# 函数用途: 明确 required 字段、旧包缺字段降级策略和未来字段扩展位置。
def _schema_policy() -> dict[str, Any]:
    return {
        "schema": "main_context_bundle.v1",
        "version": 1,
        "required_fields": list(MAIN_CONTEXT_BUNDLE_REQUIRED_FIELDS),
        "required_reader_policy": "missing_required_field_sets_self_check_false_but_reader_must_degrade",
        "optional_reader_policy": "missing_optional_field_defaults_to_empty",
        "future_fields": "additive_only_until_v2",
        "reserved": {},
    }


# LLM: _owner_model reserves the future subagent owner shape without changing main-agent behavior.
# 函数用途: 记录 owner_type、root/parent run 和 task workspace refs，后续子代理复用内核时不用另起一套。
def _owner_model(request: Any, home_paths: Any | None) -> dict[str, Any]:
    run_id = _text(getattr(request, "run_id", ""))
    return {
        "owner_type": _text(getattr(request, "owner_type", "")) or "main_agent",
        "owner_id": _text(getattr(request, "owner_id", "")) or "root",
        "root_run_id": _text(getattr(request, "root_run_id", "")) or run_id,
        "parent_run_id": _text(getattr(request, "parent_run_id", "")),
        "task_workspace_refs": _task_workspace_refs(home_paths),
        "memory_scope": "main_agent_home",
        "reserved": {},
    }


# LLM: _run_scope records filesystem and write boundaries as machine fields, not prompt prose.
# 函数用途: 描述当前运行可见工作区、可写根、禁止根、锁定文件和路径风格。
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
        "reserved": {},
    }


# LLM: _tool_manifest separates visible tools from executable tools and records failure taxonomy.
# 函数用途: 给恢复/接管方明确工具可见范围、实际可执行范围、权限模式和常见失败分类。
def _tool_manifest(request: Any) -> dict[str, Any]:
    payload = tool_manifest_payload(
        list(_sequence(getattr(request, "tool_specs", ()))),
        allowed_tools=_string_list(getattr(request, "allowed_tools", ())),
        granted_capabilities=_string_list(getattr(request, "granted_capabilities", ())),
        owner_type=_owner_type(request),
    )
    payload["tool_specs"] = payload["tools"]
    payload["reserved"] = {}
    return payload


# LLM: _artifact_refs gives main context bundles a refs-first artifact surface.
# 函数用途: 登记本轮已知重要产物引用；只保存引用和来源，不读取产物正文。
def _artifact_refs(request: Any) -> dict[str, Any]:
    items = [
        {"ref": ref, "kind": "artifact_ref", "source": "context_bundle_request", "reserved": {}}
        for ref in _string_list(getattr(request, "artifact_refs", ()))
    ]
    return {
        "items": items,
        "collection_phase": "pre_tool_loop",
        "body_policy": "refs_only_read_explicitly",
        "reserved": {},
    }


# LLM: _acceptance_contract records completion criteria independently from natural-language prompt text.
# 函数用途: 从 task_attributes 中提取验收、约束和最近测试，缺失时显式标记 not_recorded。
def _acceptance_contract(request: Any) -> dict[str, Any]:
    attrs = getattr(request, "task_attributes", None) if isinstance(getattr(request, "task_attributes", None), dict) else {}
    items = _string_list(attrs.get("acceptance") or attrs.get("acceptance_criteria") or attrs.get("验收条件"))
    constraints = _string_list(attrs.get("constraints") or attrs.get("约束"))
    latest_tests = _string_list(attrs.get("latest_tests") or attrs.get("tests") or attrs.get("最近测试"))
    return {
        "source_status": "explicit_from_task_attributes" if items or constraints or latest_tests else "not_recorded",
        "items": items,
        "constraints": constraints,
        "latest_tests": latest_tests,
        "reserved": {},
    }


# LLM: _self_check validates the bundle's own contract and critical refs without reading large bodies.
# 函数用途: 检查必需字段、工作区、home/memory/compact 根和 prompt 预算，失败时让后续恢复降级。
def _self_check(bundle: dict[str, Any]) -> dict[str, Any]:
    checks = [
        *_required_field_checks(bundle),
        *_path_checks(bundle),
        _prompt_budget_check(bundle),
    ]
    return {
        "ok": all(item["ok"] for item in checks if item["severity"] == "hard"),
        "checks": checks,
        "reserved": {},
    }


# LLM: _required_field_checks protects schema migration from silently dropping key sections.
# 函数用途: 验证主上下文包必需顶层字段存在且是非空对象。
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


# LLM: _path_checks verifies only stable roots, never arbitrary artifact bodies.
# 函数用途: 检查 workspace、my-agent home、memory root 和 compact applies root 是否存在。
def _path_checks(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    workspace = _dict(bundle.get("workspace_refs"))
    recovery = _dict(bundle.get("recovery_refs"))
    return [
        _exists_check("workspace_root_exists", workspace.get("primary_workspace_root"), "hard"),
        _exists_check("my_agent_home_exists", workspace.get("my_agent_home"), "soft"),
        _exists_check("memory_root_exists", workspace.get("memory_root"), "soft"),
        _exists_check("compact_applies_root_exists", recovery.get("compact_applies_root"), "soft"),
    ]


# LLM: _prompt_budget_check enforces the short prompt-section contract.
# 函数用途: 确认注入 prompt 的摘要没有超过预算；完整 JSON 仍保存在文件里。
def _prompt_budget_check(bundle: dict[str, Any]) -> dict[str, Any]:
    budget = _dict(bundle.get("prompt_budget"))
    actual = int(budget.get("prompt_section_chars", 0) or 0)
    maximum = int(budget.get("max_prompt_section_chars", MAIN_CONTEXT_BUNDLE_PROMPT_MAX_CHARS) or 0)
    return {"name": "prompt_section_within_budget", "ok": actual <= maximum, "severity": "hard"}


# LLM: _prompt_budget keeps the prompt section bounded independently from full JSON size.
# 函数用途: 记录 prompt 摘要预算和实际长度，防止 refs-only 包后续膨胀成大 prompt。
def _prompt_budget(chars: int) -> dict[str, Any]:
    return {
        "max_prompt_section_chars": MAIN_CONTEXT_BUNDLE_PROMPT_MAX_CHARS,
        "prompt_section_chars": int(chars),
        "full_json_policy": "persist_to_file_not_prompt",
        "reserved": {},
    }


# LLM: _task_workspace_refs points future owner models at home task roots without forcing reads.
# 函数用途: 返回任务工作区根路径引用；没有 home 时保持空，旧测试和 no-save 场景可兼容。
def _task_workspace_refs(home_paths: Any | None) -> dict[str, str]:
    if home_paths is None:
        return {}
    owner_tasks = Path(getattr(home_paths, "owner_tasks_dir", home_paths.workspace_tasks_dir)).resolve()
    legacy_tasks = Path(home_paths.workspace_tasks_dir).resolve()
    return {
        "owner_tasks_root": str(owner_tasks),
        "legacy_workspace_tasks_root": str(legacy_tasks),
    }


# LLM: _exists_check returns diagnostic records instead of raising on missing paths.
# 函数用途: 构造路径存在性自检条目，路径为空时按软失败记录。
def _exists_check(name: str, value: object, severity: str) -> dict[str, Any]:
    path = _text(value)
    return {"name": name, "ok": bool(path and Path(path).exists()), "severity": severity, "path": path}


# LLM: _paths normalizes path-like config fields relative to the workspace.
# 函数用途: 将字符串/Path 列表转成去重绝对路径列表，忽略空值。
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


# LLM: _string_list normalizes config/user fields that may be strings or lists.
# 函数用途: 把单值或列表转成去空白字符串列表。
def _string_list(value: object) -> list[str]:
    return [_text(item) for item in _sequence(value) if _text(item)]


# LLM: _sequence makes scalar config values safe for list-style processing.
# 函数用途: 将 None、字符串、tuple/list 等输入规整成可迭代列表。
def _sequence(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple | set):
        return list(value)
    return [value]


# LLM: _dict protects bundle builders from unexpected scalar JSON fields.
# 函数用途: 非 dict 值统一返回空对象。
def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


# LLM: _text keeps bundle JSON fields stable and stripped.
# 函数用途: 将任意值转为去空白字符串，None 变空字符串。
def _text(value: object) -> str:
    return str(value or "").strip()


# LLM: _owner_type is a small helper for tool permission wording.
# 函数用途: 读取 owner_type，缺省按 main_agent 处理。
def _owner_type(request: Any) -> str:
    return _text(getattr(request, "owner_type", "")) or "main_agent"


__all__ = ["finalize_context_bundle_contracts", "main_context_contract_sections"]
