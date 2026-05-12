# LLM: Root subagent seed contract repair keeps create_subagents thin.
# 模块用途: 当主模型创建 root/coordinator 时，从原始用户 prompt 补回不可丢的文件和层级合同。

from __future__ import annotations

from ..subagents.required_file_terms import (
    forbidden_file_terms_from_text,
    required_file_terms_from_text,
)
from ..subagents.services.hierarchy_context import (
    _hierarchy_contract_segments,
    hierarchy_contract_present,
)


# LLM: explicit_root_goal_with_user_contract prevents root seeds from losing user hard contracts.
# 函数用途: 主代理创建 root/coordinator 时，如果模型把原始 prompt 总结缩水，就补回 required/forbidden/层级锚点。
def explicit_root_goal_with_user_contract(agent, goal: str) -> str:
    source = str(getattr(agent, "_current_user_prompt", "") or "")
    if not source:
        return goal
    blocks = _missing_user_contract_blocks(goal, source)
    if not blocks:
        return goal
    return "\n\n".join([goal, *blocks])


# LLM: _missing_user_contract_blocks renders compact root-seed inheritance blocks from raw user text.
# 函数用途: 只补机器可解析的小清单，不复制完整用户 prompt，避免 root seed 过长。
def _missing_user_contract_blocks(goal: str, source: str) -> list[str]:
    blocks: list[str] = []
    required = _missing_terms(_required_file_terms(source), goal)
    if required:
        blocks.append("用户原始必需文件/产物名（必须原样交付，root seed 不得总结缩水）：" + "、".join(required))
    forbidden = _missing_terms(_forbidden_file_terms(source), goal)
    if forbidden:
        blocks.append("用户原始禁止文件/反例名（禁止创建，不得当成 required_files）：" + "、".join(forbidden))
    hierarchy = _missing_hierarchy_contracts(goal, source)
    if hierarchy:
        blocks.append("用户原始层级/命名约束（必须原样遵守）：" + " / ".join(hierarchy))
    return blocks


# LLM: _required_file_terms keeps root seed contract parsing aligned with context bundles.
# 函数用途: 从原始用户 prompt 提取正向交付文件名，作为 root 创建时的兜底合同。
def _required_file_terms(text: str) -> list[str]:
    return required_file_terms_from_text(
        text,
        extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html",
    )


# LLM: _forbidden_file_terms keeps root seed negative examples separate from deliverables.
# 函数用途: 从原始用户 prompt 提取禁止文件名，避免第一层 root goal 直接丢失 forbidden_files。
def _forbidden_file_terms(text: str) -> list[str]:
    return forbidden_file_terms_from_text(
        text,
        extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html",
    )


# LLM: _missing_terms returns source terms absent from the model-written root goal.
# 函数用途: 对 required/forbidden 文件清单做大小写无关缺项判断，保持原顺序。
def _missing_terms(terms: list[str], goal: str) -> list[str]:
    lowered = str(goal or "").lower()
    return [term for term in terms if term.lower() not in lowered]


# LLM: _missing_hierarchy_contracts keeps exact hierarchy naming visible in the root task.
# 函数用途: 从原始用户 prompt 复制短层级锚点，避免 root 创建时只留下“4层”但丢 depth 命名细节。
def _missing_hierarchy_contracts(goal: str, source: str) -> list[str]:
    missing: list[str] = []
    for item in _hierarchy_contract_segments(source):
        if not hierarchy_contract_present(item, goal):
            missing.append(item)
    return missing
