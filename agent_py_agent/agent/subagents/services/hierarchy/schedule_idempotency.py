# LLM: 递归复用只依赖原合同、父级和权限范围，优先于待提交准备对象；不能重选已有孩子或从名称文本推断身份。
# 模块用途: 在原层级入口优先复用孩子，只有新项重新冻结并提交原准备对象。
from __future__ import annotations

"""Schedule idempotency using shared TaskStatus lifecycle sets."""

from dataclasses import dataclass
from typing import Any

from ....common.value_parsing import text_value
from ...models import SUBAGENT_DISPATCH_READY_STATUSES, SUBAGENT_REUSABLE_STATUSES, task_status_in
from ..base import CreateRunParams, PreparedSubagentRun
from ..contract_identity import (
    idempotency_contract_identity_from_context_packs,
    repair_contract_identity_from_context_packs,
)


@dataclass(frozen=True)
class ScheduledChildResolution:
    task: Any
    reused: bool = False


# LLM: 原持久复用始终优先；只有新项沿当前父状态重新冻结并提交同一 task，不能给复用项改模型或生成新身份。
# 函数用途: 解析递归项的复用或创建，在同批父 revision 变化后安全提交原准备对象。
def resolve_scheduled_child(manager: Any, params: CreateRunParams, *, prepared: PreparedSubagentRun | None = None) -> ScheduledChildResolution:
    existing = find_reusable_scheduled_child(manager, params)
    if existing is not None:
        return ScheduledChildResolution(task=existing, reused=True)
    if prepared is not None:
        prepared = manager.base_service.refreeze_run(params=params, prepared=prepared)
        return ScheduledChildResolution(task=manager.create_run(params=params, prepared=prepared), reused=False)
    return ScheduledChildResolution(task=manager.create_run(params=params), reused=False)


def find_reusable_scheduled_child(manager: Any, params: CreateRunParams):
    parent_id = text_value(params.parent_id)
    if not parent_id:
        return None
    for child in reversed(_direct_children(manager, parent_id)):
        if _same_schedule_contract(child, params):
            return child
    return None


def created_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if not item.reused]


def reused_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if item.reused]


def dispatchable_scheduled_children(resolutions: list[ScheduledChildResolution]) -> list[Any]:
    return [item.task for item in resolutions if task_status_in(_status(item.task), SUBAGENT_DISPATCH_READY_STATUSES)]


# LLM: 仅同一显式幂等合同可以忽略双方已证明为系统生成的名称；旧记录缺来源或用户明确名称仍按原比较。
# 函数用途: 核对递归复用的父级、角色、写范围和原合同，防止默认编号改变造成重复派工。
def _same_schedule_contract(task: Any, params: CreateRunParams) -> bool:
    if not task_status_in(_status(task), SUBAGENT_REUSABLE_STATUSES):
        return False
    if text_value(getattr(task, "parent_id", "")) != text_value(params.parent_id):
        return False
    if text_value(params.root_id) and text_value(getattr(task, "root_id", "")) != text_value(params.root_id):
        return False
    if _normalized_role(getattr(task, "role", "")) != _normalized_role(params.role):
        return False
    idempotency_identity = idempotency_contract_identity_from_context_packs(params.context_packs)
    same_idempotency = bool(idempotency_identity) and idempotency_identity == idempotency_contract_identity_from_context_packs(getattr(task, "context_packs", []))
    if not _same_schedule_name(task, params, explicit_idempotency=same_idempotency):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    repair_identity = repair_contract_identity_from_context_packs(params.context_packs)
    if repair_identity:
        return repair_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == repair_identity
    if idempotency_identity:
        return idempotency_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == idempotency_identity
    return False


# LLM: 命名来源在原创建参数规范化前由宿主写入，普通 attributes 被上游移除；不使用 regex、角色词表或编号猜测。
# 函数用途: 保留用户改名的原合同，只让已证明的默认名在同一显式幂等请求重放时忽略展示序号。
def _same_schedule_name(task: Any, params: CreateRunParams, *, explicit_idempotency: bool) -> bool:
    if _normalized_name(getattr(task, "agent_name", "")) == _normalized_name(params.agent_name):
        return True
    if not explicit_idempotency:
        return False
    for value in (getattr(task, "attributes", {}), params.attributes):
        origin = value.get("host_agent_name_origin.v1") if isinstance(value, dict) else None
        if not isinstance(origin, dict) or origin.get("explicit") is not False:
            return False
    return True


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


def _external_write_roots(task: Any) -> tuple[str, ...]:
    task_dir = _normalized_path(getattr(task, "task_dir", ""))
    roots = []
    for raw in getattr(task, "allowed_write_roots", []) or []:
        root = _normalized_path(raw)
        if root and root != task_dir:
            roots.append(root)
    return tuple(sorted(dict.fromkeys(roots)))


def _params_extra_write_roots(params: CreateRunParams) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(_normalized_path(item) for item in params.extra_write_roots or [] if text_value(item))))


def _normalized_role(value: object) -> str:
    return text_value(value).replace("_", "-").casefold()


def _normalized_name(value: object) -> str:
    return text_value(value).replace("_", "-").casefold()


def _normalized_path(value: object) -> str:
    text = text_value(value)
    if text == "/":
        return text
    return text.rstrip("/")


def _status(task: Any) -> str:
    return text_value(getattr(task, "status", "")).upper()
