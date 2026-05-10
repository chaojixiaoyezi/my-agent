# LLM: Hierarchy context helpers keep inherited parent scope bounded and reusable.
# 模块用途: 为层级调度生成继承目标、父级摘要和 thought，避免 scheduler 主流程膨胀。

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..models import SubAgentTask

if TYPE_CHECKING:
    from .hierarchy_scheduler import HierarchyChildSpec


# LLM: inherited_hierarchy_thought gives descendants relevant context without expanding sibling scope.
# 函数用途: 把父级相关边界压成 child thought；避免某个 child 误拿到其它 sibling 的目标。
def inherited_hierarchy_thought(parent: SubAgentTask, *, child_goal: str = "") -> str:
    parts = [
        f"执行由 {parent.id} 派生的层级子任务。",
        "必须把下一层 goal 写成自包含任务，包含目标、产物路径、工具边界和验收条件。",
        "当前节点只执行自己的 goal；父级摘要只作边界/验收背景，不要展开父级其它 sibling 任务。",
    ]
    parent_context = relevant_parent_context(parent.goal, child_goal) or clip_parent_context(parent.goal)
    if parent_context:
        parts.append(f"父级相关边界摘要：{parent_context}")
    if parent.thought:
        parts.append(f"父级补充：{clip_parent_context(parent.thought)}")
    return "\n".join(parts)


# LLM: scheduled_child_goal makes model-shortened child goals self-contained before persistence.
# 函数用途: 当模型只给下一层短目标时，把父级目标/边界补入 goal，确保产物路径和限制能继续传给孙级。
def scheduled_child_goal(parent: SubAgentTask, spec: HierarchyChildSpec) -> str:
    goal = str(spec.goal or "").strip()
    if not parent.goal or goal_carries_parent_scope(parent, goal):
        return goal
    inherited = _inherited_goal_context(parent, goal)
    if not inherited:
        return goal
    return "\n\n".join([goal, inherited])


# LLM: goal_carries_parent_scope avoids duplicating inherited blocks when a model already wrote a complete child goal.
# 函数用途: 判断 child goal 是否已经包含父级写入根或继承边界；包含时不再追加父级摘要。
def goal_carries_parent_scope(parent: SubAgentTask, goal: str) -> bool:
    if "继承父级目标/边界" in goal:
        return True
    relevant = relevant_parent_context(parent.goal, goal)
    if _missing_relevant_file_terms(relevant, goal):
        return False
    roots = [str(item or "").rstrip("/") for item in parent.allowed_write_roots]
    return bool(goal and any(root and root in goal for root in roots))


# LLM: _missing_relevant_file_terms keeps exact parent deliverable contracts from being dropped.
# 函数用途: child goal 只写目录但漏掉父级明确文件名时，要求继续继承父级相关片段。
def _missing_relevant_file_terms(relevant: str, goal: str) -> bool:
    terms = _file_terms(relevant)
    if not terms:
        return False
    lowered = str(goal or "").lower()
    return any(term.lower() not in lowered for term in terms)


# LLM: _file_terms extracts explicit filenames from compact parent context.
# 函数用途: 提取 solution.py、test_solution.py、README.md 等验收文件名，用于判断下层交接是否完整。
def _file_terms(text: str) -> list[str]:
    pattern = r"(?<![\w.-])[\w.-]+\.(?:py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html)(?![\w.-])"
    return list(dict.fromkeys(re.findall(pattern, str(text or ""), flags=re.IGNORECASE)))


# LLM: relevant_parent_context keeps inherited scope focused on the current child instead of every sibling.
# 函数用途: 从父级目标里挑出和当前 child goal 最相关的片段，避免多分支任务树重复膨胀。
def relevant_parent_context(parent_goal: str, child_goal: str, *, limit: int = 800) -> str:
    segments = _parent_goal_segments(parent_goal)
    tokens = _scope_tokens(child_goal)
    if not segments or not tokens:
        return ""
    scored = [(_segment_score(segment, tokens), segment) for segment in segments]
    best = max((score for score, _ in scored), default=0)
    if best <= 0:
        return ""
    selected = [segment for score, segment in scored if score == best]
    path_segments = [segment for segment in segments if "/" in segment]
    file_path_segments = [segment for segment in path_segments if re.search(r"\.[a-zA-Z0-9]{1,8}\b", segment)]
    extra_path_segments = file_path_segments if len(file_path_segments) == 1 else path_segments
    if len(extra_path_segments) == 1 and extra_path_segments[0] not in selected:
        selected.append(extra_path_segments[0])
    return clip_parent_context(" ".join(selected), limit=limit)


# LLM: _inherited_goal_context renders boundary text that models can safely pass to descendants.
# 函数用途: 生成 child goal 的继承块；只放当前相关片段和写入根，不把父级完整 sibling 目标塞进去。
def _inherited_goal_context(parent: SubAgentTask, child_goal: str) -> str:
    lines = [
        "继承父级目标/边界（仅用于目录/权限/验收，不代表当前子任务要执行父级全部目标）：",
        "当前子任务只执行上方 goal，不要展开父级其它 sibling 目标。",
    ]
    roots = _write_root_lines(parent)
    if roots:
        lines.append("允许写入根：")
        lines.extend(roots)
    relevant = relevant_parent_context(parent.goal, child_goal)
    if relevant:
        lines.extend(["相关父级片段：", relevant])
    elif parent.goal:
        lines.extend(["父级摘要：", clip_parent_context(parent.goal)])
    return "\n".join(lines)


# LLM: _write_root_lines keeps inherited filesystem boundaries explicit without copying artifacts.
# 函数用途: 把父级允许写入根格式化为短列表，供下层继续使用。
def _write_root_lines(parent: SubAgentTask) -> list[str]:
    roots: list[str] = []
    for raw in parent.allowed_write_roots:
        text = str(raw or "").strip()
        if text and text not in roots:
            roots.append(text)
    return [f"- {item}" for item in roots]


# LLM: _parent_goal_segments splits mixed Chinese/English task descriptions into matchable chunks.
# 函数用途: 把父级长目标按换行和常见标点切成片段，便于筛掉无关 sibling。
def _parent_goal_segments(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[\n。；;]+", str(text or ""))
        if item.strip()
    ]


# LLM: _scope_tokens extracts model-stable identifiers such as arithmetic or leaf_worker_text.
# 函数用途: 从 child goal 提取用于匹配父级片段的关键词，跳过 solution/test 这类泛词。
def _scope_tokens(text: str) -> set[str]:
    stop = {"solution", "test", "tests", "readme", "file", "files", "write", "root", "goal"}
    tokens = re.findall(r"[a-zA-Z0-9_]+", str(text or "").lower())
    return {token for token in tokens if len(token) >= 3 and token not in stop}


# LLM: _segment_score ranks parent segments by current child identifiers.
# 函数用途: 计算父级片段和 child 目标的相关度；只把最高相关片段继承给 child。
def _segment_score(segment: str, tokens: set[str]) -> int:
    lowered = segment.lower()
    return sum(1 for token in tokens if token in lowered)


# LLM: clip_parent_context bounds inherited text so deep hierarchies do not explode prompts.
# 函数用途: 限制父级上下文长度；保留开头关键信息，避免层级越深 token 越失控。
def clip_parent_context(text: str, *, limit: int = 1600) -> str:
    compact = str(text or "").strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "...[truncated]"
