"""LLM: Validate supplied plan bindings and explicitly declared output scope.

模块用途: 创建子代理前校验可选 exact covers 与可选产物路径；未绑定 child 不冒充现有 Todo。
"""

from __future__ import annotations

from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from .create_constraints import delegated_product_write_roots, is_relative_to
from .dispatch_progress_seed import planned_dispatch_contract

PLANNED_DELEGATION_ERROR_CODE = "SUBAGENT_PLANNED_DELEGATION_INVALID"


# LLM: This is a structural creation gate, not a quality or completion gate.
# Exact plan bindings need a canonical plan, while explicitly declared sibling
# output collisions are provable without one. Both return one atomic not-started
# result; goals, titles, filesystem contents, and undeclared writes are ignored.
# 函数用途: 汇总调用方主动提供的计划绑定、产物越界和同批产物范围冲突；未声明的范围不猜测。
def planned_delegation_failure(
    agent: object,
    items: list,
    allowed_tool_values: list[list[str] | None],
) -> dict[str, object] | None:
    del allowed_tool_values
    contract = planned_dispatch_contract(agent, items)
    roots = delegated_product_write_roots(agent)
    invalid_output_files: list[dict[str, object]] = []
    if contract is not None:
        for index, item in enumerate(items):
            params = getattr(item, "params", None)
            params = params if isinstance(params, dict) else {}
            outputs = string_list(params.get("output_files"), TOOL_TEXT_LIST_OPTIONS)
            if not outputs:
                continue
            issues = _output_scope_issues(outputs, roots)
            if issues:
                invalid_output_files.append({"index": index, "issues": issues})
    overlapping_output_files = _overlapping_output_files(items, roots)
    if (
        (contract is None or bool(contract.get("valid")))
        and not invalid_output_files
        and not overlapping_output_files
    ):
        return None
    repairs = _required_repairs(
        contract or {},
        roots=roots,
        invalid_output_files=invalid_output_files,
        overlapping_output_files=overlapping_output_files,
    )
    return {
        "ok": False,
        "error_code": PLANNED_DELEGATION_ERROR_CODE,
        "error": (
            "本次派工提供了无效的计划绑定、越出当前工作区的产物声明，或同批互相重叠的"
            "显式产物范围；本批没有创建任何子代理。"
        ),
        "planned_dispatch": contract or {},
        "allowed_workspace_roots": list(roots),
        "invalid_output_files": invalid_output_files,
        "overlapping_output_files": overlapping_output_files,
        "next_action": {
            "action": "repair_planned_delegation_and_retry",
            "required_repairs": repairs,
            "retry_tool": "create_subagents",
            "preserve_user_constraints": True,
        },
    }


# LLM: Only caller-declared local output paths participate. Comparison is by
# normalized path segments, so `/core` and `/corex` stay disjoint while equal or
# ancestor/descendant scopes conflict. Missing declarations remain open-world.
# 函数用途: 找出同一批不同 item 主动声明的相同目录或祖先/子目录范围。
def _overlapping_output_files(
    items: list,
    roots: tuple[str, ...],
) -> list[dict[str, object]]:
    resolved_roots = _resolved_roots(roots)
    comparison_root = (
        resolved_roots[0]
        if resolved_roots
        else Path("/__my_agent_declared_workspace__")
    )
    declared: list[tuple[int, str, Path]] = []
    for index, item in enumerate(items):
        params = getattr(item, "params", None)
        params = params if isinstance(params, dict) else {}
        outputs = string_list(params.get("output_files"), TOOL_TEXT_LIST_OPTIONS)
        for output in outputs:
            normalized = output.strip().replace("\\", "/")
            if not normalized or "://" in normalized:
                continue
            try:
                path = Path(normalized).expanduser()
                resolved = (
                    path.resolve(strict=False)
                    if path.is_absolute()
                    else (comparison_root / path).resolve(strict=False)
                )
            except (OSError, RuntimeError):
                continue
            declared.append((index, output, resolved))
    conflicts: list[dict[str, object]] = []
    for left_position, (left_index, left_raw, left_path) in enumerate(declared):
        for right_index, right_raw, right_path in declared[left_position + 1 :]:
            if left_index == right_index:
                continue
            if not (
                left_path == right_path
                or is_relative_to(left_path, right_path)
                or is_relative_to(right_path, left_path)
            ):
                continue
            conflicts.append({
                "left_index": left_index,
                "left_output_file": left_raw,
                "right_index": right_index,
                "right_output_file": right_raw,
                "reason": "same_or_nested_output_scope",
            })
    return conflicts


# LLM: Proposed write paths are resolved only against inherited structured
# roots. Optional output_files remain delivery/collision hints, never permission
# grants or proof of a complete child write set.
# 函数用途: 模型可选择声明产物；一旦声明，找出其中越出父级工作区、非本地或无法解析的路径。
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
    invalid_output_files: list[dict[str, object]],
    overlapping_output_files: list[dict[str, object]],
) -> list[dict[str, object]]:
    repairs: list[dict[str, object]] = []
    if (
        contract.get("unknown_covers_by_item")
        or contract.get("unavailable_covers_by_item")
        or contract.get("duplicate_covers")
    ):
        repairs.append({
            "action": "repair_or_remove_invalid_covers",
            "open_target_ids": list(contract.get("open_target_ids") or []),
            "unbound_item_indexes": list(contract.get("unbound_item_indexes") or []),
            "reason": (
                "covers 可省略；只有确实对应同一工作时才绑定 open exact id。返工已关闭项应先用 "
                "task_progress 以 correction=true 重开原 id，再绑定原 id；不能拿无关 open id 顶替。"
            ),
        })
    if invalid_output_files:
        repairs.append({
            "action": "move_output_files_inside_parent_workspace",
            "allowed_workspace_roots": list(roots),
            "item_indexes": [int(item["index"]) for item in invalid_output_files],
            "reason": "新实现必须位于当前父级工作区；兄弟目录不能靠 goal 或 capability grant 扩权。",
        })
    if overlapping_output_files:
        repairs.append({
            "action": "split_overlapping_output_scopes",
            "conflicts": [dict(item) for item in overlapping_output_files],
            "reason": (
                "同批每个 item 只声明自己独占的最窄文件或目录。宽范围初始化若包含兄弟负责的子目录，"
                "应先单独完成，或把声明缩到真正独占的文件后再并行。"
            ),
        })
    return repairs


__all__ = ["PLANNED_DELEGATION_ERROR_CODE", "planned_delegation_failure"]
