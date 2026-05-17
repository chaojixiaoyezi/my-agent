# LLM: Hierarchy context helpers keep inherited parent scope bounded and reusable.
# 模块用途: 为层级调度生成继承目标、父级摘要和 thought，避免 scheduler 主流程膨胀。

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..models import SubAgentTask
from ..required_file_terms import forbidden_file_terms_from_text, required_file_terms_from_text

if TYPE_CHECKING:
    from .hierarchy_scheduler import HierarchyChildSpec

_LINEAGE_CONTRACT_RE = re.compile(r"小+傻妞-\*")
_STRUCTURED_FIELD_RE = re.compile(r"^\s*(?:[-*]\s*)?(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<tail>.*)$")
_HIERARCHY_CONTRACT_FIELDS = frozenset({"hierarchy_contracts", "lineage_contracts", "delegation_contracts"})


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


# LLM: scheduled_child_goal makes child goals self-contained while reflecting actual write roots.
# 函数用途: 生成 child goal；补入父级相关边界，但允许路径作为委派/验收上下文继续传给下一层。
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
    if "继承父级目标/边界" in goal:
        return True
    if _missing_relevant_file_terms(parent.goal, goal):
        return False
    if _missing_forbidden_file_terms(parent.goal, goal):
        return False
    if _missing_hierarchy_contract_terms(parent.goal, goal):
        return False
    if _missing_capability_contract_terms(parent.goal, goal):
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


# LLM: _missing_forbidden_file_terms keeps negative file contracts from vanishing during summarization.
# 函数用途: child goal 只写“禁止改名”但漏掉具体 product.html/output.json 反例时，强制追加父级继承块。
def _missing_forbidden_file_terms(relevant: str, goal: str) -> bool:
    terms = _forbidden_file_terms(relevant)
    if not terms:
        return False
    lowered = str(goal or "").lower()
    return any(term.lower() not in lowered for term in terms)


# LLM: _missing_hierarchy_contract_terms keeps explicit depth-chain requirements alive across delegation.
# 函数用途: 父级要求 4 层链路/孙孙节点时，child goal 不能只带路径就丢掉这类协作约束。
def _missing_hierarchy_contract_terms(parent_goal: str, goal: str) -> bool:
    contracts = _hierarchy_contract_segments(parent_goal)
    if not contracts:
        return False
    return any(not hierarchy_contract_present(segment, goal) for segment in contracts)


# LLM: _missing_capability_contract_terms prevents delegated agents from dropping tool grants.
# 函数用途: 父级写明 controlled_exec、capability_request、trash 或 refs 时，child goal 必须继续携带这些硬约束。
def _missing_capability_contract_terms(parent_goal: str, goal: str) -> bool:
    required = _capability_contract_terms(parent_goal)
    if not required:
        return False
    lowered = str(goal or "").lower()
    return any(term not in lowered for term in required)


# LLM: _file_terms extracts explicit filenames from compact parent context.
# 函数用途: 提取 solution.py、test_solution.py、README.md 等验收文件名，用于判断下层交接是否完整。
def _file_terms(text: str) -> list[str]:
    return required_file_terms_from_text(
        text,
        extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html",
    )


# LLM: _forbidden_file_terms keeps negative filename examples visible without promoting them to deliverables.
# 函数用途: 提取 product.html/legacy.html 这类禁止反例，给下层明确的“不要创建/不要改名成”清单。
def _forbidden_file_terms(text: str) -> list[str]:
    return forbidden_file_terms_from_text(
        text,
        extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html",
    )


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
def _inherited_goal_context(
    parent: SubAgentTask,
    child_goal: str,
    *,
    write_roots: list[str] | None = None,
) -> str:
    lines = [
        "继承父级目标/边界（仅用于目录/权限/验收，不代表当前子任务要执行父级全部目标）：",
        "当前子任务只执行上方 goal，不要展开父级其它 sibling 目标。",
    ]
    roots = _write_root_lines(parent, write_roots=write_roots)
    if roots:
        lines.append("允许写入根：")
        lines.extend(roots)
    file_terms = _file_terms(parent.goal)
    if file_terms:
        lines.append("required_files:")
        lines.extend(f"- {item}" for item in file_terms)
    forbidden_file_terms = _forbidden_file_terms(parent.goal)
    if forbidden_file_terms:
        lines.append("forbidden_files:")
        lines.extend(f"- {item}" for item in forbidden_file_terms)
    hierarchy_contracts = _hierarchy_contract_segments(parent.goal)
    if hierarchy_contracts:
        lines.append("hierarchy_contracts:")
        lines.extend(f"- {item}" for item in hierarchy_contracts)
    capability_contracts = _capability_contract_segments(parent.goal)
    if capability_contracts:
        lines.append("父级能力/工具/安全约束（必须原样遵守，不能改名或缩水）：")
        lines.extend(f"- {item}" for item in capability_contracts)
    relevant = relevant_parent_context(parent.goal, child_goal)
    if relevant:
        lines.extend(["相关父级片段：", relevant])
    elif parent.goal:
        lines.extend(["父级摘要：", clip_parent_context(parent.goal)])
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


# LLM: _parent_goal_segments splits mixed Chinese/English task descriptions into matchable chunks.
# 函数用途: 把父级长目标按换行和常见标点切成片段，便于筛掉无关 sibling。
def _parent_goal_segments(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[\n。；;]+", str(text or ""))
        if item.strip()
    ]


# LLM: _hierarchy_contract_segments extracts structured delegation-shape constraints.
# 函数用途: 只读取 hierarchy_contracts/lineage_contracts/delegation_contracts 机器字段，不从“4层/孙孙”等自然语言猜。
def _hierarchy_contract_segments(text: str) -> list[str]:
    values: list[str] = []
    active = False
    for raw in str(text or "").splitlines():
        active, items = _hierarchy_contract_line(raw, active=active)
        values.extend(items)
    return _dedupe_contracts(clip_parent_context(item, limit=500) for item in values)


# LLM: _hierarchy_contract_line parses one hierarchy contract line.
# 函数用途: 返回 active 状态和本行 hierarchy contract 项，避免主解析函数嵌套。
def _hierarchy_contract_line(raw: str, *, active: bool) -> tuple[bool, list[str]]:
    line = raw.strip()
    field = _structured_contract_field(line)
    if field:
        is_active = field[0] in _HIERARCHY_CONTRACT_FIELDS
        return is_active, _contract_items(field[1]) if is_active and field[1] else []
    if active and line.startswith(("-", "*")):
        return active, _contract_items(line.lstrip("-* "))
    return False, []


# LLM: hierarchy_contract_present verifies exact structured hierarchy terms before accepting a summary.
# 函数用途: 如果父级合同写了“小小小傻妞-*”这类精确前缀，子级不能只写 depth=3 就算已继承。
def hierarchy_contract_present(segment: str, goal: str) -> bool:
    goal_text = str(goal or "").lower()
    lineage_terms = _lineage_contract_terms(segment)
    if lineage_terms:
        return all(term.lower() in goal_text for term in lineage_terms)
    anchor = _segment_anchor(segment)
    return bool(anchor and anchor in goal_text)


# LLM: _lineage_contract_terms extracts configurable-looking lineage prefixes from a contract line.
# 函数用途: 捕获“小傻妞-* / 小小傻妞-* / 更深前缀”等精确命名合同，避免模型摘要把层级名字改错。
def _lineage_contract_terms(segment: str) -> list[str]:
    terms: list[str] = []
    for match in _LINEAGE_CONTRACT_RE.finditer(str(segment or "")):
        value = match.group(0)
        if value not in terms:
            terms.append(value)
    return terms


# LLM: _capability_contract_segments extracts non-droppable tool/shell safety requirements.
# 函数用途: 把 controlled_exec 授权、命令范围、输出外置和 trash 行为作为硬继承合同传给下层。
def _capability_contract_segments(text: str) -> list[str]:
    keywords = _capability_contract_keywords()
    return [
        clip_parent_context(segment, limit=500)
        for segment in _parent_goal_segments(text)
        if any(keyword in segment.lower() for keyword in keywords)
    ]


# LLM: _capability_contract_terms returns stable anchors for goal completeness checks.
# 函数用途: 提取父级能力合同关键词，判断 child goal 是否已经完整携带。
def _capability_contract_terms(text: str) -> list[str]:
    lowered = str(text or "").lower()
    return [keyword for keyword in _capability_contract_keywords() if keyword in lowered]


# LLM: _capability_contract_keywords centralizes hard delegation terms for future tool gateways.
# 函数用途: 集中维护不能在层级转述中丢失的能力/工具/安全字段名。
def _capability_contract_keywords() -> tuple[str, ...]:
    return (
        "controlled_exec",
        "capability_request",
        "requested_tools",
        "requested_commands",
        "path_scope",
        "output_budget",
        "task_trash",
        "move_to_task_trash",
        "stdout_ref",
        "audit_ref",
        "trash_manifest_ref",
        "grant",
    )


# LLM: _segment_anchor gives structured contract checks a stable short token.
# 函数用途: 用结构化字段片段的短 token 判断 child goal 是否已经携带同类层级合同，避免重复追加。
def _segment_anchor(segment: str) -> str:
    lowered = segment.lower()
    tokens = re.findall(r"[a-zA-Z0-9_]+|小+傻妞-\*", lowered)
    return tokens[0] if tokens else lowered[:24]


# LLM: _structured_contract_field recognizes protocol fields only.
# 函数用途: 解析 hierarchy_contracts/capability_contracts 这类机器字段，避免中文标题进入代码规则。
def _structured_contract_field(line: str) -> tuple[str, str] | None:
    match = _STRUCTURED_FIELD_RE.match(line)
    if not match:
        return None
    return match.group("field").strip().lower(), match.group("tail").strip()


# LLM: _contract_items tokenizes compact structured contract lists.
# 函数用途: 支持 `hierarchy_contracts: depth=1 | depth=2` 和 bullet 两种写法。
def _contract_items(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    separator = "|" if "|" in text else "、"
    return [item.strip() for item in text.split(separator) if item.strip()]


# LLM: _dedupe_contracts preserves contract order.
# 函数用途: 去重 hierarchy/capability 合同片段，避免继承块重复膨胀。
def _dedupe_contracts(values) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items


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
