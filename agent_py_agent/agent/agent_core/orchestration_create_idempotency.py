# LLM: create_subagents idempotency keeps repeated model calls from growing duplicate child trees.
# 模块用途: 根据同父级命名子代理的结构化事实复用已有 run，并生成下一步调度 run ids。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts.state_machine import RunStateFacts, can_dispatch
from ..subagents.services.base import CreateRunParams
from ..subagents.services.idempotency_contract_identity import (
    idempotency_contract_identity_from_context_packs,
)
from ..subagents.services.repair_contract_identity import (
    repair_contract_identity_from_context_packs,
)

_REUSABLE_STATUSES = {"PLANNING", "PENDING", "RUNNING", "DONE", "COMPLETED", "BLOCKED", "PAUSED"}
_GENERIC_AGENT_NAMES = {
    "",
    "general",
    "worker",
    "subagent",
    "agent",
    "小傻妞",
    "小傻妞-worker",
    "小傻妞-general",
    "小傻妞-researcher",
    "小傻妞-writer",
    "小傻妞-tester",
}
_GENERIC_LINEAGE_ROLES = {
    "worker",
    "general",
    "researcher",
    "writer",
    "tester",
    "bug-finder",
    "coordinator",
    "leaf-worker",
}


# LLM: CreateTaskResolution records whether a task was newly created or reused.
# 类用途: 让 create_subagents payload 同时表达 created/reused/dispatch 三种事实，避免父级靠自然语言猜。
@dataclass(frozen=True)
class CreateTaskResolution:
    task: Any
    reused: bool = False


# LLM: resolve_create_run returns an existing same-parent named child when safe.
# 函数用途: 同一 parent/root 下同名、同角色且仍可复用的 child 已存在时直接返回它；否则调用 manager 创建新 run。
def resolve_create_run(manager: Any, params: CreateRunParams) -> CreateTaskResolution:
    existing = find_reusable_named_child(manager, params)
    if existing is not None:
        return CreateTaskResolution(task=existing, reused=True)
    return CreateTaskResolution(task=manager.create_run(params=params), reused=False)


# LLM: find_reusable_named_child makes idempotency a structured state lookup, not a prompt instruction.
# 函数用途: 查询已有同名 sibling run；只复用未失败/未废弃的明确命名子代理，避免无限重复创建。
def find_reusable_named_child(manager: Any, params: CreateRunParams):
    repair_identity = repair_contract_identity_from_context_packs(params.context_packs)
    if repair_identity:
        return find_reusable_repair_child(manager, params, repair_identity)
    idempotency_identity = idempotency_contract_identity_from_context_packs(params.context_packs)
    if idempotency_identity:
        return find_reusable_idempotency_child(manager, params, idempotency_identity)
    work_scope_key = _work_scope_key(params)
    if work_scope_key:
        return find_reusable_work_scope_child(manager, params, work_scope_key)
    name = _normalized_name(params.agent_name)
    if _is_generic_agent_name(name):
        return None
    if _is_indexed_generic_agent_name(name):
        return None
    return None


# LLM: find_reusable_work_scope_child reuses only explicit output/input scope contracts.
# 函数用途: 同一父级重复派“同一产物范围”的 worker 时复用已有 run；普通 goal 文本仍不参与。
def find_reusable_work_scope_child(manager: Any, params: CreateRunParams, work_scope_key: str):
    for task in reversed(_safe_list_runs(manager)):
        if _same_work_scope(task, params, work_scope_key):
            return task
    return None


# LLM: find_reusable_repair_child keys repair reuse by failure refs, not by the generic repair display name.
# 函数用途: 同一失败 run/目标产物复用同一个 repair owner；不同修复范围即使同名也创建新 run。
def find_reusable_repair_child(manager: Any, params: CreateRunParams, repair_identity: tuple[object, ...]):
    for task in reversed(_safe_list_runs(manager)):
        if _same_repair_scope(task, params, repair_identity):
            return task
    return None


# LLM: find_reusable_idempotency_child keys generic replay safety by explicit idempotency contract.
# 函数用途: 同一父级下重复提交同一个结构化幂等合同才复用；普通 goal 文本不参与。
def find_reusable_idempotency_child(manager: Any, params: CreateRunParams, idempotency_identity: tuple[object, ...]):
    for task in reversed(_safe_list_runs(manager)):
        if _same_idempotency_scope(task, params, idempotency_identity):
            return task
    return None


# LLM: created_tasks filters resolution records for schedule envelopes and audits.
# 函数用途: 返回本次真正新建的 run，供 payload.created_run_ids 和 typed envelope 使用。
def created_tasks(resolutions: list[CreateTaskResolution]) -> list[Any]:
    return [item.task for item in resolutions if not item.reused]


# LLM: reused_tasks filters resolution records for parent recovery hints.
# 函数用途: 返回本次复用的已有 run，帮助 root 下一步直接 dispatch 而不是再 create。
def reused_tasks(resolutions: list[CreateTaskResolution]) -> list[Any]:
    return [item.task for item in resolutions if item.reused]


# LLM: dispatchable_tasks returns only children that should be started now.
# 函数用途: 已 DONE/RUNNING/BLOCKED 的复用 run 仍展示给 root，但不会被建议重复 dispatch。
def dispatchable_tasks(tasks: list[Any]) -> list[Any]:
    return [
        task
        for task in tasks
        if can_dispatch(RunStateFacts(status=_status(task), verification_status=_verification(task)))
    ]


# LLM: _same_repair_scope compares stable repair_contract identity only.
# 函数用途: 防止 “小傻妞-验收修复” 这种固定名字把不同失败对象误合并。
def _same_repair_scope(task: Any, params: CreateRunParams, repair_identity: tuple[object, ...]) -> bool:
    if _status(task) not in _REUSABLE_STATUSES:
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _compatible_role(getattr(task, "role", ""), params.role):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    return repair_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == repair_identity


# LLM: _same_idempotency_scope compares explicit idempotency contracts inside parent/root scope.
# 函数用途: 幂等复用只看结构化合同，避免普通 goal 文案成为系统事实来源。
def _same_idempotency_scope(task: Any, params: CreateRunParams, idempotency_identity: tuple[object, ...]) -> bool:
    if _status(task) not in _REUSABLE_STATUSES:
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _compatible_role(getattr(task, "role", ""), params.role):
        return False
    if _normalized_name(getattr(task, "agent_name", "")) != _normalized_name(params.agent_name):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    return idempotency_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == idempotency_identity


def _same_work_scope(task: Any, params: CreateRunParams, work_scope_key: str) -> bool:
    if _status(task) not in _REUSABLE_STATUSES:
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _compatible_role(getattr(task, "role", ""), params.role):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    attrs = getattr(task, "attributes", {}) or {}
    return isinstance(attrs, dict) and _text(attrs.get("work_scope_key")) == work_scope_key


def _work_scope_key(params: CreateRunParams) -> str:
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    return _text(attrs.get("work_scope_key"))


# LLM: _requested_root_id treats an omitted root as top-level create scope, not a literal empty root_id.
# 函数用途: 顶层 create_run 会把 root_id 写成自己的 run id；模型没传 root_id 时按 parent/name 去重即可。
def _requested_root_id(params: CreateRunParams) -> str:
    return _text(params.root_id)


# LLM: _compatible_role avoids reusing an explicit checker as a worker with the same display name.
# 函数用途: 两边都有明确 role 且不同就不复用；空值或通用 worker 保持宽容。
def _compatible_role(existing: object, requested: object) -> bool:
    existing_text = _text(existing)
    requested_text = _text(requested)
    if not existing_text or not requested_text:
        return True
    return existing_text == requested_text


# LLM: _external_write_roots ignores the task's private runtime dir when comparing user deliverable scope.
# 函数用途: 从 allowed_write_roots 中去掉 task_dir，只比较用户/父级授权的产物根。
def _external_write_roots(task: Any) -> tuple[str, ...]:
    task_dir = _text(getattr(task, "task_dir", ""))
    roots = []
    for raw in getattr(task, "allowed_write_roots", []) or []:
        root = _normalized_path(raw)
        if root and root != _normalized_path(task_dir):
            roots.append(root)
    return tuple(sorted(dict.fromkeys(roots)))


# LLM: _params_extra_write_roots is the create-time version of external write roots.
# 函数用途: 规范化 CreateRunParams.extra_write_roots，确保同一产物根顺序不同也能复用。
def _params_extra_write_roots(params: CreateRunParams) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(_normalized_path(item) for item in params.extra_write_roots or [] if _text(item))))


# LLM: _normalized_path keeps write-root comparison stable without resolving nonexistent paths.
# 函数用途: 清理路径字符串里的尾部斜杠和空格；不访问文件系统。
def _normalized_path(value: object) -> str:
    text = _text(value)
    if text == "/":
        return text
    return text.rstrip("/")


# LLM: _safe_list_runs keeps mocked managers and old adapters from crashing create_subagents.
# 函数用途: manager 没有 list_runs 或读取失败时关闭复用逻辑，回到原创建行为。
def _safe_list_runs(manager: Any) -> list[Any]:
    try:
        runs = manager.list_runs()
    except (AttributeError, OSError, TypeError, ValueError):
        return []
    return list(runs or [])


# LLM: _normalized_name keeps Chinese role names stable while trimming model whitespace.
# 函数用途: 规范化 agent_name；不做 aggressive 语义聚类，避免误合并不同子任务。
def _normalized_name(value: object) -> str:
    return _text(value).casefold()


# LLM: _is_generic_agent_name covers role/index names generated by the new lineage contract.
# 函数用途: 小傻妞-worker-1 这种系统名仍要按 goal/write-root 合同复用，不能只按名字误合并不同任务。
def _is_generic_agent_name(value: object) -> bool:
    name = _normalized_name(value)
    if name in _GENERIC_AGENT_NAMES:
        return True
    return False


# LLM: _is_indexed_generic_agent_name detects generated role/index lineage names.
# 函数用途: 区分“小傻妞-worker”和“小傻妞-worker-1”，让编号成为 sibling 身份的一部分。
def _is_indexed_generic_agent_name(value: object) -> bool:
    name = _normalized_name(value)
    parts = name.split("-")
    if len(parts) < 3 or not parts[-1].isdigit():
        return False
    prefix = parts[0]
    role = "-".join(parts[1:-1])
    return _is_lineage_prefix(prefix) and role in _GENERIC_LINEAGE_ROLES


# LLM: _is_lineage_prefix recognizes generated 小傻妞 depth markers.
# 函数用途: 判断名字第一段是否为“小...傻妞”，用于默认名合同复用。
def _is_lineage_prefix(value: str) -> bool:
    text = str(value or "").strip()
    return len(text) >= 2 and text.endswith("傻妞") and set(text[:-2]) == {"小"}


# LLM: _status normalizes task lifecycle values from dataclasses or mocks.
# 函数用途: 读取状态并转成大写字符串，供复用和调度过滤。
def _status(task: Any) -> str:
    return _text(getattr(task, "status", "")).upper()


# LLM: _verification mirrors the shared state-machine facts without importing subagent task models.
# 函数用途: 读取 task.verification_status 供 dispatchable 判断兼容未来状态扩展。
def _verification(task: Any) -> str:
    return _text(getattr(task, "verification_status", "")).upper()


# LLM: _text guards against MagicMock truthiness and non-string fields.
# 函数用途: 把字段安全转换为去空格字符串。
def _text(value: object) -> str:
    return str(value or "").strip()
