# LLM: Hierarchy context helpers keep inherited parent scope bounded and reusable.
# 模块用途: 为层级调度生成继承目标、父级摘要和 thought，避免 scheduler 主流程膨胀。

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import SubAgentTask

if TYPE_CHECKING:
    from .hierarchy_scheduler import HierarchyChildSpec

_INHERITED_ATTRIBUTE_FIELDS = (
    "capability_contracts",
    # LLM: conversation refs are inherited as machine fields so descendants can raise events without guessing chat context.
    "conversation_task_id",
    "conversation_thread_id",
    "acceptance_required_tools",
    "controlled_exec_contract_required",
    "domain_scopes",
    "forbidden_child_scopes",
    "forbidden_files",
    "hierarchy_contracts",
    "required_files",
    "required_tool_evidence",
    "required_tools",
)


# LLM: inherited_hierarchy_thought gives descendants relevant context without expanding sibling scope.
# 函数用途: 把父级相关边界压成 child thought；避免某个 child 误拿到其它 sibling 的目标。
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


# LLM: scheduled_child_goal makes child goals self-contained while reflecting actual write roots.
# 函数用途: 生成 child goal；补入父级收口上下文继续传给下一层。
def scheduled_child_goal(
    parent: SubAgentTask,
    spec: HierarchyChildSpec,
    *,
    write_roots: list[str] | None = None,
) -> str:
    goal = str(spec.goal or "").strip()
    if not parent.goal or goal_carries_parent_scope(parent, goal):
        return goal
    inherited = _inherited_goal_context(parent, goal, write_roots=write_roots)
    if not inherited:
        return goal
    return "\n\n".join([goal, inherited])


# LLM: goal_carries_parent_scope avoids duplicating inherited blocks when a model already wrote a complete child goal.
# 函数用途: 判断 child goal 是否已经包含父级写入根或继承边界；包含时不再追加父级摘要。
def goal_carries_parent_scope(parent: SubAgentTask, goal: str) -> bool:
    del parent
    return _has_inherited_parent_context_flag(goal)


# LLM: _has_inherited_parent_context_flag accepts only the exact inherited context marker.
# 函数用途: 判断 child goal 是否已经带继承块；这个标记由系统生成，不从用户自然语言推断。
def _has_inherited_parent_context_flag(goal: str) -> bool:
    return any(raw.strip() == "inherited_parent_context=true" for raw in str(goal or "").splitlines())


# LLM: relevant_parent_context no longer ranks prose segments as machine facts.
# 函数用途: 保留旧调用接口；普通父级目标不由代码挑片段，完整摘要只作为模型阅读背景。
def relevant_parent_context(parent_goal: str, child_goal: str, *, limit: int = 800) -> str:
    del parent_goal, child_goal, limit
    return ""


# LLM: _inherited_goal_context renders boundary text that models can safely pass to descendants.
# 函数用途: 生成 child goal 的继承块；只放当前相关片段和写入根，不把父级完整 sibling 目标塞进去。
def _inherited_goal_context(
    parent: SubAgentTask,
    child_goal: str,
    *,
    write_roots: list[str] | None = None,
) -> str:
    lines = [
        "inherited_parent_context=true",
        "继承父级收口，不代表当前子任务要执行父级全部目标）：",
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


# LLM: _write_root_lines keeps inherited filesystem boundaries explicit without copying artifacts.
# 函数用途: 把父级允许写入根格式化为短列表，供下层继续使用。
def _write_root_lines(parent: SubAgentTask, *, write_roots: list[str] | None = None) -> list[str]:
    roots: list[str] = []
    for raw in parent.allowed_write_roots if write_roots is None else write_roots:
        text = str(raw or "").strip()
        if text and text not in roots:
            roots.append(text)
    return [f"- {item}" for item in roots]


# LLM: _relevant_file_terms reads inherited deliverables from parent attributes.
# 函数用途: 下层继承 required_files 机器字段；普通父级 goal 文本不再参与文件合同判断。
def _relevant_file_terms(parent: SubAgentTask, child_goal: str) -> list[str]:
    del child_goal
    return _attribute_list(parent, "required_files")


# LLM: inherited_hierarchy_attributes forwards parent machine contracts through the task record.
# 函数用途: 创建 child run 时把 required_files/domain_scopes 等合同写入 attributes，后续层级不用读 goal。
def inherited_hierarchy_attributes(parent: SubAgentTask, spec: HierarchyChildSpec) -> dict[str, object]:
    attrs = dict(getattr(spec, "attributes", {}) or {})
    parent_attrs = _task_attributes(parent)
    for field in _INHERITED_ATTRIBUTE_FIELDS:
        if field not in attrs and parent_attrs.get(field) not in (None, "", [], {}):
            attrs[field] = parent_attrs[field]
    return attrs


# LLM: _attribute_list normalizes persisted hierarchy contracts without parsing prose.
# 函数用途: 从 task.attributes 读取字符串列表字段，兼容单字符串和列表。
def _attribute_list(task: SubAgentTask, field: str) -> list[str]:
    return _dedupe_contracts(_list_items(_task_attributes(task).get(field)))


# LLM: _task_attributes safely reads the task attributes dict.
# 函数用途: attributes 缺失或格式不对时返回空对象，让调用方保守不继承。
def _task_attributes(task: SubAgentTask) -> dict[str, object]:
    attrs = getattr(task, "attributes", {}) or {}
    return attrs if isinstance(attrs, dict) else {}


# LLM: _list_items normalizes attribute values that were already parsed before persistence.
# 函数用途: 支持 attributes 里写字符串、列表或元组；不再拆普通中文句子。
def _list_items(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [text for item in value if (text := str(item or "").strip())]
    text = str(value or "").strip()
    return [text] if text else []


# LLM: _dedupe_contracts preserves contract order.
# 函数用途: 去重 hierarchy/capability 合同片段，避免继承块重复膨胀。
def _dedupe_contracts(values) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


# LLM: clip_parent_context bounds inherited text so deep hierarchies do not explode prompts.
# 函数用途: 限制父级上下文长度；保留开头关键信息，避免层级越深 token 越失控。
def clip_parent_context(text: str, *, limit: int = 1600) -> str:
    compact = str(text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "...[truncated]"
