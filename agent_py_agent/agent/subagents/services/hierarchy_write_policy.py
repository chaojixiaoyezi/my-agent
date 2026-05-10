# LLM: Hierarchy write policy separates product-write authority from report-writing tools.
# 模块用途: 判断层级 child 是否能继承最终产物写入根，避免 scheduler 主流程继续膨胀。

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import _extract_write_dirs

if TYPE_CHECKING:
    from ..models import SubAgentTask

_PRODUCT_WRITE_ROLE_MARKERS = ("worker", "writer", "implementer", "developer", "coder")
_REPORT_ONLY_ROLE_MARKERS = (
    "coordinator",
    "researcher",
    "bug_finder",
    "tester",
    "acceptor",
    "checker",
    "reviewer",
    "critic",
    "reporter",
)


# LLM: ScheduledWriteRootRequest bundles role and intent facts for the write-root policy.
# 类用途: 把 role、requested roots 和 leaf 写入意图打包，避免策略函数使用散参数。
@dataclass(frozen=True)
class ScheduledWriteRootRequest:
    role: str
    spec_role: str
    requested_roots: list[str]
    leaf_write_intent: bool


# LLM: ChildWriteRootRequest gathers parent, explicit, and model-written path sources.
# 类用途: 计算 child 可委派写入根时，把父级继承、spec.extra_write_roots 和 spec.goal 里的路径放在同一入口。
@dataclass(frozen=True)
class ChildWriteRootRequest:
    parent: SubAgentTask
    spec_goal: str
    explicit_roots: list[str]


# LLM: scheduled_child_extra_write_roots grants product roots only to product-writing roles.
# 函数用途: 报告/检查/验收/协调类角色只写自己的工单目录；worker/writer/leaf_worker 才继承产品产物目录。
def scheduled_child_extra_write_roots(request: ScheduledWriteRootRequest) -> list[str]:
    normalized = str(request.role or request.spec_role or "").strip().lower()
    if any(marker in normalized for marker in _REPORT_ONLY_ROLE_MARKERS):
        return []
    if any(marker in normalized for marker in _PRODUCT_WRITE_ROLE_MARKERS):
        return list(dict.fromkeys(request.requested_roots))
    if request.leaf_write_intent:
        return list(dict.fromkeys(request.requested_roots))
    return []


# LLM: requested_child_write_roots keeps model-provided deliverable paths available for leaf grants.
# 函数用途: child spec 自己写出产物路径时也纳入候选根；是否授权仍由角色/写入意图策略决定。
def requested_child_write_roots(request: ChildWriteRootRequest) -> list[str]:
    roots: list[str] = []
    for item in [
        *request.explicit_roots,
        *inherited_extra_write_roots(request.parent),
        *_extract_write_dirs(request.spec_goal),
    ]:
        text = str(item or "").rstrip("/")
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: inherited_extra_write_roots forwards product roots for delegation without copying parent internals.
# 函数用途: 从父节点授权根和 goal 路径提取可委派产物根，跳过父工单目录本身。
def inherited_extra_write_roots(parent: SubAgentTask) -> list[str]:
    parent_task_dir = str(parent.task_dir or "").rstrip("/")
    roots: list[str] = []
    for item in [*parent.allowed_write_roots, *_extract_write_dirs(parent.goal)]:
        text = str(item or "").rstrip("/")
        if text and text != parent_task_dir and text not in roots:
            roots.append(str(item))
    return roots
