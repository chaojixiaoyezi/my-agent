
from __future__ import annotations

"""Hierarchy prompt context and structured inheritance metadata.

Machine state such as inherited parent context lives in task attributes; the goal
text remains only the model-readable task handoff.
"""

from typing import TYPE_CHECKING

from ...models import SubAgentTask

if TYPE_CHECKING:
    from .scheduler import HierarchyChildSpec

_INHERITED_ATTRIBUTE_FIELDS = (
    "audit_deadline_unix",
    "audit_guarantee",
    "audit_objective",
    "audit_run_epoch",
    "audit_window_seconds",
    "capability_contracts",
    "conversation_request_id",
    "conversation_task_id",
    "conversation_thread_id",
    "acceptance_required_tools",
    "controlled_exec_contract_required",
    "forbidden_files",
    "hierarchy_contracts",
    "required_files",
    "required_tool_evidence",
    "required_tools",
    "skill_snapshot_refs",
)
_INHERITED_CONTEXT_ATTRIBUTE = "inherited_parent_context"


def inherited_hierarchy_thought(parent: SubAgentTask, *, child_goal: str = "") -> str:
    parts = [
        f"执行由 {parent.id} 派生的层级子任务。",
        "必须把下一层 goal 写成自包含任务，包含目标、产物路径、工具边界和验收条件。",
        "当前节点只执行自己的 goal；父级收口背景，不要展开父级其它 sibling 任务。",
    ]
    parent_context = relevant_parent_context(parent.goal, child_goal) or clip_parent_context(parent.goal)
    if parent_context:
        parts.append(f"父级相关边界摘要：{parent_context}")
    if parent.thought:
        parts.append(f"父级补充：{clip_parent_context(parent.thought)}")
    return "\n".join(parts)


def scheduled_child_goal(
    parent: SubAgentTask,
    spec: HierarchyChildSpec,
    *,
    write_roots: list[str] | None = None,
) -> str:
    goal = str(spec.goal or "").strip()
    if not parent.goal or _spec_carries_parent_scope(spec):
        return goal
    inherited = _inherited_goal_context(parent, goal, write_roots=write_roots)
    if not inherited:
        return goal
    return "\n\n".join([goal, inherited])


def _spec_carries_parent_scope(spec: HierarchyChildSpec) -> bool:
    return dict(getattr(spec, "attributes", {}) or {}).get(_INHERITED_CONTEXT_ATTRIBUTE) is True


def relevant_parent_context(parent_goal: str, child_goal: str, *, limit: int = 800) -> str:
    del parent_goal, child_goal, limit
    return ""


def _inherited_goal_context(
    parent: SubAgentTask,
    child_goal: str,
    *,
    write_roots: list[str] | None = None,
) -> str:
    lines = [
        "继承父级目标/边界（只作为背景，不代表当前子任务要执行父级全部目标）：",
        "当前子任务只执行上方 goal，不要展开父级其它 sibling 目标。",
    ]
    roots = _write_root_lines(parent, write_roots=write_roots)
    if roots:
        lines.append("允许写入根：")
        lines.extend(roots)
    file_terms = _relevant_file_terms(parent, child_goal)
    if file_terms:
        lines.append("required_files:")
        lines.extend(f"- {item}" for item in file_terms)
    forbidden_file_terms = _attribute_list(parent, "forbidden_files")
    if forbidden_file_terms:
        lines.append("forbidden_files:")
        lines.extend(f"- {item}" for item in forbidden_file_terms)
    hierarchy_contracts = _attribute_list(parent, "hierarchy_contracts")
    if hierarchy_contracts:
        lines.append("hierarchy_contracts:")
        lines.extend(f"- {item}" for item in hierarchy_contracts)
    capability_contracts = _attribute_list(parent, "capability_contracts")
    if capability_contracts:
        lines.append("capability_contracts:")
        lines.extend(f"- {item}" for item in capability_contracts)
    return "\n".join(lines)


def _write_root_lines(parent: SubAgentTask, *, write_roots: list[str] | None = None) -> list[str]:
    roots: list[str] = []
    for raw in parent.allowed_write_roots if write_roots is None else write_roots:
        text = str(raw or "").strip()
        if text and text not in roots:
            roots.append(text)
    return [f"- {item}" for item in roots]


def _relevant_file_terms(parent: SubAgentTask, child_goal: str) -> list[str]:
    del child_goal
    return _attribute_list(parent, "required_files")


def inherited_hierarchy_attributes(parent: SubAgentTask, spec: HierarchyChildSpec) -> dict[str, object]:
    attrs = dict(getattr(spec, "attributes", {}) or {})
    parent_attrs = _task_attributes(parent)
    if parent.goal and _INHERITED_CONTEXT_ATTRIBUTE not in attrs:
        attrs[_INHERITED_CONTEXT_ATTRIBUTE] = True
    for field in _INHERITED_ATTRIBUTE_FIELDS:
        if field not in attrs and parent_attrs.get(field) not in (None, "", [], {}):
            attrs[field] = parent_attrs[field]
    return attrs


def _attribute_list(task: SubAgentTask, field: str) -> list[str]:
    return _dedupe_contracts(_list_items(_task_attributes(task).get(field)))


def _task_attributes(task: SubAgentTask) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    return attrs if isinstance(attrs, dict) else {}


def _list_items(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [text for item in value if (text := str(item or "").strip())]
    text = str(value or "").strip()
    return [text] if text else []


def _dedupe_contracts(values) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def clip_parent_context(text: str, *, limit: int = 1600) -> str:
    compact = str(text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "...[truncated]"
