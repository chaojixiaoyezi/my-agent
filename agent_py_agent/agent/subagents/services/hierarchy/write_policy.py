
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...models import SubAgentTask

@dataclass(frozen=True)
class ScheduledWriteRootRequest:
    role: str
    spec_role: str
    requested_roots: list[str]
    leaf_write_intent: bool


@dataclass(frozen=True)
class ChildWriteRootRequest:
    parent: SubAgentTask
    spec_goal: str
    explicit_roots: list[str]


def scheduled_child_extra_write_roots(request: ScheduledWriteRootRequest) -> list[str]:
    return list(dict.fromkeys(request.requested_roots))


def requested_child_write_roots(request: ChildWriteRootRequest) -> list[str]:
    roots: list[str] = []
    for item in [
        *request.explicit_roots,
        *inherited_extra_write_roots(request.parent),
    ]:
        text = str(item or "").rstrip("/")
        if text and text not in roots:
            roots.append(text)
    return roots


def inherited_extra_write_roots(parent: SubAgentTask) -> list[str]:
    parent_task_dir = str(parent.task_dir or "").rstrip("/")
    roots: list[str] = []
    authorized_roots = _non_task_allowed_roots(parent, parent_task_dir)
    for item in authorized_roots:
        text = str(item or "").rstrip("/")
        if text and text != parent_task_dir and text not in roots:
            roots.append(str(item))
    return roots


def _non_task_allowed_roots(parent: SubAgentTask, parent_task_dir: str) -> list[str]:
    roots: list[str] = []
    for item in parent.allowed_write_roots:
        text = str(item or "").rstrip("/")
        if text and text != parent_task_dir and text not in roots:
            roots.append(text)
    return roots


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
