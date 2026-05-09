# LLM: Hierarchy context helpers keep inherited parent scope bounded and reusable.
# 模块用途: 为层级调度生成继承目标、父级摘要和 thought，避免 scheduler 主流程膨胀。

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import SubAgentTask

if TYPE_CHECKING:
    from .hierarchy_scheduler import HierarchyChildSpec


# LLM: inherited_hierarchy_thought gives descendants enough context without broadening permissions.
# 函数用途: 把父级目标和提示压成 child thought，避免下一层只看到空泛编号任务。
def inherited_hierarchy_thought(parent: SubAgentTask) -> str:
    parts = [
        f"执行由 {parent.id} 派生的层级子任务。",
        "必须把下一层 goal 写成自包含任务，包含目标、产物路径、工具边界和验收条件。",
    ]
    if parent.goal:
        parts.append(f"父级目标摘要：{clip_parent_context(parent.goal)}")
    if parent.thought:
        parts.append(f"父级补充：{clip_parent_context(parent.thought)}")
    return "\n".join(parts)


# LLM: scheduled_child_goal makes model-shortened child goals self-contained before persistence.
# 函数用途: 当模型只给下一层短目标时，把父级目标/边界补入 goal，确保产物路径和限制能继续传给孙级。
def scheduled_child_goal(parent: SubAgentTask, spec: HierarchyChildSpec) -> str:
    goal = str(spec.goal or "").strip()
    if not parent.goal or goal_carries_parent_scope(parent, goal):
        return goal
    return "\n\n".join([
        goal,
        "继承父级目标/边界（必须继续传给下一层，不得改写或丢弃）：",
        clip_parent_context(parent.goal),
    ])


# LLM: goal_carries_parent_scope avoids duplicating inherited blocks when a model already wrote a complete child goal.
# 函数用途: 判断 child goal 是否已经包含父级写入根或继承边界；包含时不再追加父级摘要。
def goal_carries_parent_scope(parent: SubAgentTask, goal: str) -> bool:
    if "继承父级目标/边界" in goal:
        return True
    roots = [str(item or "").rstrip("/") for item in parent.allowed_write_roots]
    return bool(goal and any(root and root in goal for root in roots))


# LLM: clip_parent_context bounds inherited text so deep hierarchies do not explode prompts.
# 函数用途: 限制父级上下文长度；保留开头关键信息，避免层级越深 token 越失控。
def clip_parent_context(text: str, *, limit: int = 1600) -> str:
    compact = str(text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "...[truncated]"
