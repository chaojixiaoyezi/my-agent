# LLM: Duplicate-domain warnings stay soft and separate from hard hierarchy guards.
# 模块用途: 检查同父级 coordinator/tester/checker 是否重复覆盖同一业务领域，只返回 warning 不阻断调度。

from __future__ import annotations

import re
from typing import Any

from ..models import SubAgentTask
from .hierarchy_domain_terms import COORDINATION_ROLE_TOKENS, DOMAIN_STOPWORDS


# LLM: duplicate_child_domain_warnings audits repeated coordinator domains without stopping execution.
# 函数用途: 同父级重复创建 checkout/quality 等 coordinator 时写 warning，让上级 LLM 自己决定。
def duplicate_child_domain_warnings(manager: Any, parent: SubAgentTask, request: Any) -> list[str]:
    warnings: list[str] = []
    seen_domains: list[set[str]] = []
    emitted: set[str] = set()
    for child in _existing_coordination_children(manager, parent):
        seen_domains.append(_child_domain_tokens(child))
    for spec in request.child_specs:
        if not _is_coordination_like(spec):
            continue
        domains = _child_domain_tokens(spec)
        duplicate = _first_overlapping_domain(domains, seen_domains)
        if duplicate and duplicate not in emitted:
            warnings.append(f"duplicate_child_domain:{duplicate}")
            emitted.add(duplicate)
        if domains:
            seen_domains.append(domains)
    return warnings


# LLM: _existing_coordination_children reads only lightweight child task metadata.
# 函数用途: 获取当前父节点已存在的 coordinator/checker/tester 子任务；读取失败时跳过。
def _existing_coordination_children(manager: Any, parent: SubAgentTask) -> list[Any]:
    children: list[Any] = []
    for child_id in parent.child_ids:
        try:
            child = manager.load(child_id)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        if _is_coordination_like(child):
            children.append(child)
    return children


# LLM: _is_coordination_like limits duplicate warnings to planner/reviewer style roles.
# 函数用途: 只给协调/测试/验收类节点做同域 warning，避免多个同域 worker 被误报。
def _is_coordination_like(item: Any) -> bool:
    text = f"{getattr(item, 'role', '')} {getattr(item, 'agent_name', '')}".lower()
    return any(token in text for token in COORDINATION_ROLE_TOKENS)


# LLM: _child_domain_tokens extracts stable, human-named task domains from a child spec or task.
# 函数用途: 从 agent_name/role/goal 中提取 checkout、quality、catalog 等领域词，用于同父级去重。
def _child_domain_tokens(item: Any) -> set[str]:
    label_text = f"{getattr(item, 'agent_name', '')} {getattr(item, 'role', '')}".lower()
    label_tokens = _domain_tokens(label_text)
    if label_tokens:
        return label_tokens
    return _domain_tokens(_goal_domain_text(str(getattr(item, "goal", "")).lower()))


# LLM: _domain_tokens removes generic role/path words before duplicate-domain comparison.
# 函数用途: 把文本转换成领域词集合；优先使用 agent_name/role，避免共享路径导致误判。
def _domain_tokens(text: str) -> set[str]:
    tokens = re.findall(r"[a-z][a-z0-9]+", text)
    return {
        token for token in tokens
        if token not in DOMAIN_STOPWORDS and not _looks_generated_id_token(token)
    }


# LLM: _goal_domain_text strips inherited paths before goal fallback domain detection.
# 函数用途: duplicate-domain 兜底看 goal 时，去掉共享目录、文件名和继承块。
def _goal_domain_text(text: str) -> str:
    head = re.split(r"\n\s*继承父级目标/边界|\n\s*父级必需文件/产物名|\n\s*父级禁止文件/反例名", text, maxsplit=1)[0]
    without_paths = re.sub(r"(?:~|/)[^\s，。；;、)）]+", " ", head)
    return re.sub(
        r"\b[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:html?|css|js|json|md|py|txt|ya?ml)\b",
        " ",
        without_paths,
    )


# LLM: _looks_generated_id_token prevents run-id fragments from becoming business domains.
# 函数用途: 过滤 `dd1d90d8` 这类自动生成 id 片段，避免误判为同域重复。
def _looks_generated_id_token(token: str) -> bool:
    return any(char.isdigit() for char in token)


# LLM: _first_overlapping_domain keeps duplicate errors deterministic.
# 函数用途: 找出新任务领域和已有领域的第一个交集，返回稳定 warning。
def _first_overlapping_domain(domains: set[str], seen_domains: list[set[str]]) -> str:
    for seen in seen_domains:
        overlap = sorted(domains & seen)
        if overlap:
            return overlap[0]
    return ""
