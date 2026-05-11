# LLM: Hierarchy write policy keeps parent authority as a superset of descendant authority.
# 模块用途: 计算层级 child 继承的写入根；上层保留覆盖权限，具体是否亲自写由角色职责和提示词约束。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


# LLM: scheduled_child_extra_write_roots propagates authority so parents can inspect, recover, and take over.
# 函数用途: 让下一层继承父级可委派产物根；角色职责仍要求 coordinator/tester/reviewer 优先写报告并把实际产物交给 worker。
def scheduled_child_extra_write_roots(request: ScheduledWriteRootRequest) -> list[str]:
    return list(dict.fromkeys(request.requested_roots))


# LLM: requested_child_write_roots keeps model-provided deliverable paths available for delegated coverage.
# 函数用途: child spec 自己写出产物路径时也纳入候选根；最终会作为上层覆盖下层的可委派写入根。
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
    authorized_roots = _non_task_allowed_roots(parent, parent_task_dir)
    for item in authorized_roots:
        text = str(item or "").rstrip("/")
        if text and text != parent_task_dir and text not in roots:
            roots.append(str(item))
    for item in _extract_write_dirs(parent.goal):
        text = str(item or "").rstrip("/")
        if _covered_by_authorized_root(text, authorized_roots):
            continue
        if text and text != parent_task_dir and text not in roots:
            roots.append(str(item))
    return roots


# LLM: _non_task_allowed_roots keeps broad parent authority without leaking task-local internals.
# 函数用途: 从父节点 allowed_write_roots 中去掉自己的工单目录，保留用户授权的产物/共享目录。
def _non_task_allowed_roots(parent: SubAgentTask, parent_task_dir: str) -> list[str]:
    roots: list[str] = []
    for item in parent.allowed_write_roots:
        text = str(item or "").rstrip("/")
        if text and text != parent_task_dir and text not in roots:
            roots.append(text)
    return roots


# LLM: _covered_by_authorized_root avoids copying sibling file paths when a broader product root exists.
# 函数用途: 若父级已经授权 deliverables 根，就不再把 sibling 的具体 solution.py/html 路径塞进 child goal。
def _covered_by_authorized_root(candidate: str, roots: list[str]) -> bool:
    if not candidate:
        return False
    candidate_path = Path(candidate).expanduser().resolve(strict=False)
    for raw in roots:
        root = Path(str(raw)).expanduser().resolve(strict=False)
        try:
            candidate_path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
