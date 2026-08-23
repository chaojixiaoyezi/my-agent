"""LLM: Validate plan bindings and coding write sets before child persistence.

模块用途: 当当前代理已经建立结构化 Todo 时，在创建任何子代理前一次性核对 exact covers 与写入目录。
"""

from __future__ import annotations

from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from .create_constraints import (
    delegated_product_write_roots,
    is_relative_to,
    role_allows_direct_product_work,
)
from .dispatch_progress_seed import planned_dispatch_contract
from .write_guard import WRITE_SUBAGENT_TOOLS

PLANNED_DELEGATION_ERROR_CODE = "SUBAGENT_PLANNED_DELEGATION_INVALID"


# LLM: This is a structural creation gate, not a quality or completion gate. It
# activates only when an authoritative plan exists and returns data for an
# atomic not-started tool result; goals and titles never influence its verdict.
# 函数用途: 汇总计划绑定和写入集合错误，全部正确或当前没有计划时返回空。
def planned_delegation_failure(
    agent: object,
    items: list,
    allowed_tool_values: list[list[str] | None],
) -> dict[str, object] | None:
    contract = planned_dispatch_contract(agent, items)
    if contract is None:
        return None
    roots = delegated_product_write_roots(agent)
    missing_output_files: list[int] = []
    invalid_output_files: list[dict[str, object]] = []
    for index, (item, allowed_tools) in enumerate(
        zip(items, allowed_tool_values, strict=True)
    ):
        params = getattr(item, "params", None)
        params = params if isinstance(params, dict) else {}
        if not _item_requires_write_set(agent, params, allowed_tools):
            continue
        outputs = string_list(params.get("output_files"), TOOL_TEXT_LIST_OPTIONS)
        if not outputs:
            missing_output_files.append(index)
            continue
        issues = _output_scope_issues(outputs, roots)
        if issues:
            invalid_output_files.append({"index": index, "issues": issues})
    if (
        bool(contract.get("valid"))
        and not missing_output_files
        and not invalid_output_files
    ):
        return None
    repairs = _required_repairs(
        contract,
        roots=roots,
        missing_output_files=missing_output_files,
        invalid_output_files=invalid_output_files,
    )
    return {
        "ok": False,
        "error_code": PLANNED_DELEGATION_ERROR_CODE,
        "error": "当前已有结构化任务清单，但本次派工没有完整绑定计划与写入集合；本批没有创建任何子代理。",
        "planned_dispatch": contract,
        "allowed_workspace_roots": list(roots),
        "missing_output_files_indexes": missing_output_files,
        "invalid_output_files": invalid_output_files,
        "next_action": {
            "action": "repair_planned_delegation_and_retry",
            "required_repairs": repairs,
            "retry_tool": "create_subagents",
            "preserve_user_constraints": True,
        },
    }


# LLM: A write-set declaration is required only from a typed direct product
# worker that actually has filesystem write tools. Read-only, test, dependent,
# and coordinator roles retain their existing contracts.
# 函数用途: 判断这条计划内派工是否需要用 output_files 声明互斥写入范围。
def _item_requires_write_set(
    agent: object,
    params: dict[str, object],
    allowed_tools: list[str] | None,
) -> bool:
    grants = {str(item or "").strip() for item in allowed_tools or []}
    if not WRITE_SUBAGENT_TOOLS.intersection(grants):
        return False
    manager = getattr(agent, "subagents", None)
    raw_dirs = getattr(manager, "role_template_dirs", None)
    role_template_dirs = raw_dirs if isinstance(raw_dirs, (list, tuple)) else None
    return role_allows_direct_product_work(
        str(params.get("role") or "worker"),
        role_template_dirs,
    )


# LLM: Proposed write paths are resolved only against inherited structured
# roots. The special task-local output/work namespaces are resolved later by
# the canonical create policy; neither case grants sibling-directory access.
# 函数用途: 找出 output_files 中越出父级工作区、非本地或无法解析的路径。
def _output_scope_issues(
    outputs: list[str],
    roots: tuple[str, ...],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    resolved_roots = _resolved_roots(roots)
    primary_root = resolved_roots[0] if resolved_roots else None
    for output in outputs:
        normalized = output.strip().replace("\\", "/")
        if _is_task_local_output(normalized):
            continue
        if "://" in normalized:
            issues.append({"output_file": output, "reason": "non_local_output_ref"})
            continue
        if primary_root is None:
            issues.append({"output_file": output, "reason": "workspace_scope_unavailable"})
            continue
        try:
            path = Path(output).expanduser()
            resolved = (
                path.resolve(strict=False)
                if path.is_absolute()
                else (primary_root / path).resolve(strict=False)
            )
        except (OSError, RuntimeError):
            issues.append({"output_file": output, "reason": "path_resolution_failed"})
            continue
        if not any(is_relative_to(resolved, root) for root in resolved_roots):
            issues.append({
                "output_file": output,
                "resolved_path": str(resolved),
                "reason": "outside_parent_workspace",
            })
    return issues


# LLM: Root parsing is fail-closed and preserves the inherited order so relative
# paths always use the same primary workspace as the child creation policy.
# 函数用途: 把父级授权目录安全解析成 Path，供输出范围核对。
def _resolved_roots(roots: tuple[str, ...]) -> list[Path]:
    resolved: list[Path] = []
    for raw in roots:
        try:
            path = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: Only exact output/work namespace prefixes select canonical task-local
# staging. Similar names remain ordinary workspace-relative paths.
# 函数用途: 判断一条相对输出是否明确指向受管任务的 output 或 work 目录。
def _is_task_local_output(value: str) -> bool:
    while value.startswith("./"):
        value = value[2:]
    return value in {"output", "work"} or value.startswith(("output/", "work/"))


# LLM: Repair guidance is built solely from typed failure fields. It can tell
# the model which schema action to take but cannot alter the user's objective.
# 函数用途: 把计划合同错误整理成模型可逐项修正并重试的动作清单。
def _required_repairs(
    contract: dict[str, object],
    *,
    roots: tuple[str, ...],
    missing_output_files: list[int],
    invalid_output_files: list[dict[str, object]],
) -> list[dict[str, object]]:
    repairs: list[dict[str, object]] = []
    if int(contract.get("open_count") or 0) <= 0:
        repairs.append({
            "action": "extend_task_progress_before_delegation",
            "reason": "现有计划没有仍可绑定的 open 项；先用 task_progress 新增真实工作项。",
        })
    if (
        contract.get("missing_covers_indexes")
        or contract.get("unknown_covers_by_item")
        or contract.get("unavailable_covers_by_item")
        or contract.get("duplicate_covers")
    ):
        repairs.append({
            "action": "bind_each_item_to_one_open_plan_id",
            "open_target_ids": list(contract.get("open_target_ids") or []),
            "reason": "给每个 item 填写它独占负责的 covers exact id；未知、已关闭或跨 item 重复 id 不可用。",
        })
    if missing_output_files:
        repairs.append({
            "action": "declare_disjoint_output_files",
            "item_indexes": missing_output_files,
            "reason": "这些 item 有写工具并直接产出产品代码；请逐项声明互不重叠的 output_files。",
        })
    if invalid_output_files:
        repairs.append({
            "action": "move_output_files_inside_parent_workspace",
            "allowed_workspace_roots": list(roots),
            "item_indexes": [int(item["index"]) for item in invalid_output_files],
            "reason": "新实现必须位于当前父级工作区；兄弟目录不能靠 goal 或 capability grant 扩权。",
        })
    return repairs


__all__ = ["PLANNED_DELEGATION_ERROR_CODE", "planned_delegation_failure"]
