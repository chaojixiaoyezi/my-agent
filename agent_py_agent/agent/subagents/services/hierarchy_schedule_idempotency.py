# LLM: schedule_child_subagents idempotency keeps child scheduling tied to task facts, not model memory.
# 模块用途: 在层级调度创建 child 前按 parent/root/role/goal/write-root 合同复用已有 direct child。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base import CreateRunParams
from .repair_contract_identity import repair_contract_identity_from_context_packs

_REUSABLE_STATUSES = {"PLANNING", "PENDING", "RUNNING", "AWAITING_ACCEPTANCE", "DONE", "COMPLETED", "BLOCKED", "PAUSED"}
_DISPATCHABLE_STATUSES = {"PLANNING", "PENDING"}


# LLM: ScheduledChildResolution records whether schedule_child_subagents reused or created a child.
# 类用途: 给 scheduler result/payload 同时提供 created/reused/dispatch 三类机器事实。
@dataclass(frozen=True)
class ScheduledChildResolution:
    task: Any
    reused: bool = False


# LLM: resolve_scheduled_child reuses an existing direct child when the schedule contract is identical.
# 函数用途: 同父级同 root/role/goal/write-root 已有可复用 child 时返回它；否则调用 manager.create_run。
def resolve_scheduled_child(manager: Any, params: CreateRunParams) -> ScheduledChildResolution:
    existing = find_reusable_scheduled_child(manager, params)
    if existing is not None:
        return ScheduledChildResolution(task=existing, reused=True)
    return ScheduledChildResolution(task=manager.create_run(params=params), reused=False)


# LLM: find_reusable_scheduled_child scans only direct children to avoid cross-branch accidental reuse.
# 函数用途: 在 parent.child_ids 中寻找合同相同且仍可复用的 child；不读 artifact 正文。
def find_reusable_scheduled_child(manager: Any, params: CreateRunParams):
    parent_id = _text(params.parent_id)
    if not parent_id:
        return None
    for child in reversed(_direct_children(manager, parent_id)):
        if _same_schedule_contract(child, params):
            return child
    return None


# LLM: created_scheduled_children filters resolution records for result payloads.
# 函数用途: 返回本次真实新建的 child run。
def created_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if not item.reused]


# LLM: reused_scheduled_children filters resolution records for parent runner handoff.
# 函数用途: 返回本次复用的已有 child run。
def reused_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if item.reused]


# LLM: dispatchable_scheduled_children excludes completed/running/blocked reused children from rerun advice.
# 函数用途: 只返回适合下一步 dispatch 的 PLANNING/PENDING child ids。
def dispatchable_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if _status(item.task) in _DISPATCHABLE_STATUSES]


# LLM: _same_schedule_contract is strict on facts that distinguish work, tolerant of display-name drift.
# 函数用途: 判断已有 child 是否就是这次 schedule 想创建的同一件事；不靠自然语言摘要记忆。
def _same_schedule_contract(task: Any, params: CreateRunParams) -> bool:
    if _status(task) not in _REUSABLE_STATUSES:
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _text(params.root_id) and _text(getattr(task, "root_id", "")) != _text(params.root_id):
        return False
    if _normalized_role(getattr(task, "role", "")) != _normalized_role(params.role):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    # LLM: repair contracts override prose-goal comparison so one repair owner can fix/execute/verify.
    # 函数用途: 有 repair_contract 时按失败 run/目标产物复用，不因 goal 改写而拆出新 child。
    repair_identity = repair_contract_identity_from_context_packs(params.context_packs)
    if repair_identity:
        return repair_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == repair_identity
    if _normalized_goal(getattr(task, "goal", "")) != _normalized_goal(params.goal):
        return False
    return True


# LLM: _direct_children loads parent child_ids instead of scanning unrelated branches.
# 函数用途: 读取 direct child 快照；缺失/坏记录跳过，避免旧任务污染新调度。
def _direct_children(manager: Any, parent_id: str) -> list[Any]:
    try:
        parent = manager.load(parent_id)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return []
    children: list[Any] = []
    for child_id in getattr(parent, "child_ids", []) or []:
        try:
            children.append(manager.load(str(child_id)))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            continue
    return children


# LLM: _external_write_roots ignores the private task_dir when comparing user deliverable scope.
# 函数用途: 从 allowed_write_roots 中去掉 child 自己的运行目录，只比较父级/用户授权产物根。
def _external_write_roots(task: Any) -> tuple[str, ...]:
    task_dir = _normalized_path(getattr(task, "task_dir", ""))
    roots = []
    for raw in getattr(task, "allowed_write_roots", []) or []:
        root = _normalized_path(raw)
        if root and root != task_dir:
            roots.append(root)
    return tuple(sorted(dict.fromkeys(roots)))


# LLM: _params_extra_write_roots mirrors create-time extra roots for schedule contract comparison.
# 函数用途: 规范化 CreateRunParams.extra_write_roots；顺序不同不影响复用。
def _params_extra_write_roots(params: CreateRunParams) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(_normalized_path(item) for item in params.extra_write_roots or [] if _text(item))))


# LLM: _normalized_goal compares concrete task contracts without semantic guessing.
# 函数用途: 压缩空白后比较 goal；不同目标必须创建新 child。
def _normalized_goal(value: object) -> str:
    return " ".join(_text(value).split())


# LLM: _normalized_role keeps role comparison stable across underscore/dash variants.
# 函数用途: role 比较只做格式归一，不把 tester/worker 这类不同职责合并。
def _normalized_role(value: object) -> str:
    return _text(value).replace("_", "-").casefold()


# LLM: _normalized_path keeps path comparison stable without touching the filesystem.
# 函数用途: 清理路径尾斜杠，避免同一根路径因格式不同被视为不同合同。
def _normalized_path(value: object) -> str:
    text = _text(value)
    if text == "/":
        return text
    return text.rstrip("/")


# LLM: _status normalizes persisted lifecycle values.
# 函数用途: 把 task.status 转成大写字符串，供复用和 dispatch 过滤。
def _status(task: Any) -> str:
    return _text(getattr(task, "status", "")).upper()


# LLM: _text safely stringifies model/persistence fields for comparisons.
# 函数用途: 把空值和非字符串字段统一转成去空格字符串。
def _text(value: object) -> str:
    return str(value or "").strip()
