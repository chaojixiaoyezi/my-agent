# LLM: 子代理授权来自显式配置、owner 和真实父级身份；原持久幂等优先于内存准备对象，目标与产物不生成权限。
# 模块用途: 计算写范围并复用合法创建记录，仅新项沿原服务提交同一准备对象。

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...common.json_io import locked_json_path
from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ...common.value_parsing import text_value as _text
from ...contracts.state_machine import RunStateFacts, can_dispatch
from ...conversation.authority import (
    conversation_execution_cwd,
    conversation_runtime_workspace_roots,
    current_conversation_task_attributes,
)
from ...path_access_policy import (
    UNINHERITABLE_ROOT_DIRS,
    inheritable_declared_work_roots,
)
from ...runtime_context import current_subagent_run_id
from ...subagents.models import SUBAGENT_REUSABLE_STATUSES, task_status_in
from ...subagents.role_templates import role_template_snapshot_for_role
from ...subagents.services.base import CreateRunParams, PreparedSubagentRun
from ...subagents.services.contract_identity import (
    idempotency_contract_identity_from_context_packs,
    repair_contract_identity_from_context_packs,
)
from ..runner.ref_fields import params_output_refs
from .create_context import (
    is_relative_to,
    normalized_write_root,
)


def role_allows_direct_product_work(role: str, role_template_dirs: object = None) -> bool:
    normalized = str(role or "worker").strip().lower().replace("-", "_")
    if not normalized:
        return True
    snapshot = role_template_snapshot_for_role(normalized, role_template_dirs)
    if not snapshot:
        return True
    if bool(snapshot.get("can_spawn_children")):
        return False
    if bool(snapshot.get("depends_on_outputs")) or bool(snapshot.get("can_run_tests")):
        return False
    return bool(snapshot.get("can_write"))


# LLM: Explicit extra roots come only from structured parameters; goal prose
# never widens authority, while ordinary inherited workspace is added later.
# 函数用途: 读取调用方明确传入的额外写目录并去重，不解析自然语言目标。
def merged_extra_write_roots(params: dict[str, object], goal: str) -> list[str]:
    roots: list[str] = []
    del goal
    for item in string_list(params.get("extra_write_roots"), TOOL_TEXT_LIST_OPTIONS):
        text = normalized_write_root(item)
        if text and text not in roots:
            roots.append(text)
    return roots


# LLM: 普通 child 继承宿主文件范围；交付引用不是额外授权，精确 worker 只保留显式授权。
# 函数用途: 计算子代理写根，不根据 tasks/output 命名、goal、产物或输入文件推导权限。
def resolved_extra_write_roots(agent: object, params: dict[str, object], goal: str) -> list[str]:
    explicit = merged_extra_write_roots(params, goal)
    if params.get("_exact_allowed_tools") is True:
        return explicit
    return _unique_roots([*explicit, *_direct_parent_product_write_roots(agent)])


# LLM: 普通后代同享 canonical owner home；精确 Audit 父级保持原授权，不受旧 task 窄目录影响。
# 函数用途: 从真实父级身份与 owner 读取文件上界；缺 owner 的独立运行环境仍按宿主既有写根。
def _direct_parent_product_write_roots(agent: object) -> list[str]:
    return _unique_roots([*_direct_parent_declared_write_roots(agent), *_second_layer_work_roots(agent)])


# LLM: 原继承规则保持不变；第二层作用域只在它之上追加，不改动精确 worker 的既有收窄。
# 函数用途: 按原规则返回直接父级已授权的产品写根。
def _direct_parent_declared_write_roots(agent: object) -> list[str]:
    manager = getattr(agent, "subagents", None)
    current = getattr(agent, "_current_run_params", None)
    delegated_run_id = current_subagent_run_id(agent)
    run_id = delegated_run_id or str(
        getattr(current, "run_id", "") or ""
    ).strip()
    if manager is not None and run_id:
        try:
            parent = manager.load(run_id)
        except (FileNotFoundError, OSError, ValueError):
            parent = None
        if parent is not None and str(getattr(parent, "id", "") or "").strip() == run_id:
            from ...common.audit_activation import structured_audit_supervised_worker_attributes

            if not structured_audit_supervised_worker_attributes(getattr(parent, "attributes", None)):
                home_roots = _current_conversation_product_write_roots(agent)
                if home_roots:
                    return home_roots
            return _task_product_write_roots(parent)
    conversation_roots = _current_conversation_product_write_roots(agent)
    if conversation_roots:
        return conversation_roots
    raw_roots = getattr(manager, "workspace_roots", None)
    roots = list(raw_roots) if isinstance(raw_roots, (list, tuple)) else []
    primary = getattr(manager, "workspace_root", None)
    if isinstance(primary, str | Path):
        roots.insert(0, primary)
    return _unique_roots(
        str(Path(item).expanduser().resolve(strict=False))
        for item in roots
        if isinstance(item, str | Path)
    )


# LLM: Creation preflights may expose the same direct-parent product roots used
# by permission inheritance, but callers must treat them as an upper bound and
# must never widen them from goal prose or proposed output paths.
# 函数用途: 给子代理创建校验返回当前直接父级真正授权的产品写目录。
def delegated_product_write_roots(agent: object) -> tuple[str, ...]:
    return tuple(_direct_parent_product_write_roots(agent))


# LLM: 普通 child 从宿主 owner home 继承文件范围；运行账本目录不产生产品权限，精确 worker 在上游排除。
# 函数用途: 把用户完整的家目录交给普通子代理使用，不因本轮处理另一个 task 而收窄。
def _current_conversation_product_write_roots(agent: object) -> list[str]:
    attrs = current_conversation_task_attributes(agent)
    declared = _unique_roots(
        str(path)
        for raw in conversation_runtime_workspace_roots(attrs)
        if (path := _resolved_path(raw)) is not None
    )
    # LLM: 用户显式声明了工作目录时，它就是本轮唯一的产品写范围——不能再把 owner home
    # 一并继承下去。否则子代理会同时拿到 home 与目标目录两个写根，把产物写回老家而不是
    # 交付目录（2026-09-11 真机实测：子代理写根里同时出现
    # owners/local/main 与 /tmp/ma-eval/port-echo-python）。
    # 未声明时维持原语义：普通 child 仍从 owner home 继承文件范围，行为不变。
    if declared:
        return declared
    home = _resolved_path(getattr(getattr(agent, "home_paths", None), "owner_home_dir", None))
    if home is not None:
        return [str(home)]
    return []


# LLM: 文件系统根级目录与"可继承工作根"判据收敛在 path_access_policy，本模块只做子代理视角的复用，
#   不再自己维护第二份常量或第二套 parents 遍历。
# 常量用途: 不得作为子代理第二层作用域自动继承的根级目录（唯一权威在 path_access_policy）。
_UNINHERITABLE_ROOT_DIRS = UNINHERITABLE_ROOT_DIRS


# LLM: 管理员 full-access 解除 owner 墙后，子代理仍只继承"父代理当时真正工作的那个目录及其子树"，
#   而不是父代理的 full-access 档位；普通用户有 owner 墙时不追加，行为完全不变。
#   来源必须是宿主写入的 conversation_execution_cwd，模型无法用它自己的一句话改写。
# 函数用途: 返回可以下发给子代理的第二层工作目录作用域（管理员在 owner 墙外工作时）。
def _second_layer_work_roots(agent: object) -> list[str]:
    tools = getattr(agent, "tools", None)
    # owner 墙存在（普通用户/默认管理员）→ 不追加任何第二层作用域；
    # 只有管理员显式选了 full-access、owner 墙被解除时（owner_scope_root 为空）才派生。
    if _resolved_path(getattr(tools, "owner_scope_root", None)) is not None:
        return []
    attrs = current_conversation_task_attributes(agent)
    candidates = [conversation_execution_cwd(attrs), *conversation_runtime_workspace_roots(attrs)]
    return inheritable_declared_work_roots(
        candidates,
        owner_home=getattr(getattr(agent, "home_paths", None), "owner_home_dir", ""),
    )


# LLM: 精确或无 owner 的父级只继承已授权的产品根；运行归档不能凭目录结构生成文件权限。
# 函数用途: 从直接父级写根剔除内部代理记录；没有产品授权就返回空。
def _task_product_write_roots(task: object) -> list[str]:
    internal_roots = [
        _resolved_path(getattr(task, "task_dir", "")),
        _resolved_path(getattr(task, "agent_run_workspace_dir", "")),
    ]
    roots: list[str] = []
    for raw in getattr(task, "allowed_write_roots", None) or []:
        path = _resolved_path(raw)
        if path is None or any(
            internal is not None and is_relative_to(path, internal)
            for internal in internal_roots
        ):
            continue
        roots.append(str(path))
    return _unique_roots(roots)


# LLM: Path parsing failures are absence of authority, never permission.
# 函数用途: 将一个结构化路径安全解析为绝对路径。
def _resolved_path(value: object) -> Path | None:
    if not isinstance(value, str | Path) or not str(value).strip():
        return None
    try:
        return Path(value).expanduser().resolve(strict=False)
    except OSError:
        return None


def explicit_root_missing_write_root_error(agent: object, params: dict[str, object], goal: str) -> str:
    del agent, params, goal
    return ""



# LLM: 写根去重保持首次出现顺序，不能做路径父子折叠而意外扩大或缩小权限。
# 函数用途: 清理并按原顺序去重一组路径文字。
def _unique_roots(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


_GENERIC_AGENT_NAMES = {
    "",
    "general",
    "worker",
    "subagent",
    "agent",
    "child",
    "agent-d1-worker",
    "agent-d1-general",
    "agent-d1-researcher",
    "agent-d1-writer",
    "agent-d1-tester",
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
_INDEXED_SYSTEM_AGENT_RE = re.compile(r"^agent-d\d+-(?P<role>[a-z0-9_-]+)-(?P<index>\d+)$")


@dataclass(frozen=True)
class CreateTaskResolution:
    task: Any
    reused: bool = False


# LLM: 原幂等锁和持久复用优先于内存准备对象；准备只透传到唯一创建入口，不为已存在孩子重冻或重新选模型。
# 函数用途: 原子解析本项复用或创建，只有未找到原任务时才提交宿主准备对象。
def resolve_create_run(manager: Any, params: CreateRunParams, *, prepared: PreparedSubagentRun | None = None) -> CreateTaskResolution:
    if _has_reusable_create_identity(params):
        workspace = getattr(manager, "workspace", None)
        if isinstance(workspace, str | Path):
            # Reuse and creation must be one transaction.  The persistence
            # layer already serializes an individual run file, but that does
            # not make the preceding "does this logical run exist?" scan
            # atomic.  A single owner-local guard avoids both thread and
            # process races without creating one lock file per historical
            # work scope.
            guard = Path(workspace) / ".create-run-idempotency.guard"
            with locked_json_path(guard):
                return _resolve_create_run_unlocked(manager, params, prepared=prepared)
    return _resolve_create_run_unlocked(manager, params, prepared=prepared)


# LLM: 调用方持原创建/幂等边界；复用直接返回 canonical，新的准备对象先按当前父代次重新冻结，再交原 manager 创建。
# 函数用途: 保留已有子代理，否则提交同一待创建对象；同批前序孩子更新父 revision 不会误杀后序项。
def _resolve_create_run_unlocked(
    manager: Any,
    params: CreateRunParams,
    *,
    prepared: PreparedSubagentRun | None = None,
) -> CreateTaskResolution:
    existing = find_reusable_named_child(manager, params)
    if existing is not None:
        return CreateTaskResolution(task=existing, reused=True)
    if prepared is not None:
        prepared = manager.base_service.refreeze_run(params=params, prepared=prepared)
        return CreateTaskResolution(task=manager.create_run(params=params, prepared=prepared), reused=False)
    return CreateTaskResolution(task=manager.create_run(params=params), reused=False)


def _has_reusable_create_identity(params: CreateRunParams) -> bool:
    return bool(
        repair_contract_identity_from_context_packs(params.context_packs)
        or idempotency_contract_identity_from_context_packs(params.context_packs)
        or _work_scope_key(params)
    )


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


def find_reusable_work_scope_child(manager: Any, params: CreateRunParams, work_scope_key: str):
    for task in reversed(_safe_list_runs(manager)):
        if _same_work_scope(task, params, work_scope_key):
            return task
    return None


def find_reusable_repair_child(manager: Any, params: CreateRunParams, repair_identity: tuple[object, ...]):
    for task in reversed(_safe_list_runs(manager)):
        if _same_repair_scope(task, params, repair_identity):
            return task
    return None


def find_reusable_idempotency_child(manager: Any, params: CreateRunParams, idempotency_identity: tuple[object, ...]):
    for task in reversed(_safe_list_runs(manager)):
        if _same_idempotency_scope(task, params, idempotency_identity):
            return task
    return None


def created_tasks(resolutions: list[CreateTaskResolution]) -> list[Any]:
    return [item.task for item in resolutions if not item.reused]


def reused_tasks(resolutions: list[CreateTaskResolution]) -> list[Any]:
    return [item.task for item in resolutions if item.reused]


def dispatchable_tasks(tasks: list[Any]) -> list[Any]:
    return [
        task
        for task in tasks
        if can_dispatch(RunStateFacts(status=_status(task), verification_status=_verification(task)))
    ]


def _same_repair_scope(task: Any, params: CreateRunParams, repair_identity: tuple[object, ...]) -> bool:
    if not task_status_in(_status(task), SUBAGENT_REUSABLE_STATUSES):
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _matching_role(getattr(task, "role", ""), params.role):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    return repair_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == repair_identity


def _same_idempotency_scope(task: Any, params: CreateRunParams, idempotency_identity: tuple[object, ...]) -> bool:
    if not task_status_in(_status(task), SUBAGENT_REUSABLE_STATUSES):
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _matching_role(getattr(task, "role", ""), params.role):
        return False
    if not _same_idempotency_display_identity(
        getattr(task, "agent_name", ""),
        params.agent_name,
    ):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    return idempotency_contract_identity_from_context_packs(getattr(task, "context_packs", [])) == idempotency_identity


def _same_work_scope(task: Any, params: CreateRunParams, work_scope_key: str) -> bool:
    if not task_status_in(_status(task), SUBAGENT_REUSABLE_STATUSES):
        return False
    if _text(getattr(task, "parent_id", "")) != _text(params.parent_id):
        return False
    if _requested_root_id(params) and _text(getattr(task, "root_id", "")) != _requested_root_id(params):
        return False
    if not _matching_role(getattr(task, "role", ""), params.role):
        return False
    if _external_write_roots(task) != _params_extra_write_roots(params):
        return False
    attrs = getattr(task, "attributes", {}) or {}
    return isinstance(attrs, dict) and _text(attrs.get("work_scope_key")) == work_scope_key


def _work_scope_key(params: CreateRunParams) -> str:
    attrs = params.attributes if isinstance(params.attributes, dict) else {}
    return _text(attrs.get("work_scope_key"))


def _requested_root_id(params: CreateRunParams) -> str:
    return _text(params.root_id)


def _matching_role(existing: object, requested: object) -> bool:
    existing_text = _text(existing)
    requested_text = _text(requested)
    if not existing_text or not requested_text:
        return True
    return existing_text == requested_text


def _external_write_roots(task: Any) -> tuple[str, ...]:
    task_dir = _text(getattr(task, "task_dir", ""))
    roots = []
    for raw in getattr(task, "allowed_write_roots", []) or []:
        root = _normalized_path(raw)
        if root and root != _normalized_path(task_dir):
            roots.append(root)
    return tuple(sorted(dict.fromkeys(roots)))


def _params_extra_write_roots(params: CreateRunParams) -> tuple[str, ...]:
    return tuple(sorted(dict.fromkeys(_normalized_path(item) for item in params.extra_write_roots or [] if _text(item))))


def _normalized_path(value: object) -> str:
    text = _text(value)
    if text == "/":
        return text
    return text.rstrip("/")


def _safe_list_runs(manager: Any) -> list[Any]:
    try:
        runs = manager.list_runs()
    except (AttributeError, OSError, TypeError, ValueError):
        return []
    return list(runs or [])


def _normalized_name(value: object) -> str:
    return _text(value).casefold()


# LLM: Display ordinals are presentation identities, not part of an explicit
# idempotency contract. Generic system siblings may have different numeric
# suffixes while the structured contract, parent, role and write scope remain
# the same; custom names still require exact equality.
# 函数用途: 比较幂等复用时的展示名；系统 worker 编号可不同，用户自定义名称必须完全一致。
def _same_idempotency_display_identity(existing: object, requested: object) -> bool:
    existing_name = _normalized_name(existing)
    requested_name = _normalized_name(requested)
    if existing_name == requested_name:
        return True
    return _is_indexed_generic_agent_name(existing_name) and _is_indexed_generic_agent_name(
        requested_name
    )


def _is_generic_agent_name(value: object) -> bool:
    name = _normalized_name(value)
    if name in _GENERIC_AGENT_NAMES:
        return True
    return False


def _is_indexed_generic_agent_name(value: object) -> bool:
    name = _normalized_name(value)
    match = _INDEXED_SYSTEM_AGENT_RE.match(name)
    if not match:
        return False
    return match.group("role") in _GENERIC_LINEAGE_ROLES


def _status(task: Any) -> str:
    return _text(getattr(task, "status", ""))


def _verification(task: Any) -> str:
    return _text(getattr(task, "verification_status", ""))
