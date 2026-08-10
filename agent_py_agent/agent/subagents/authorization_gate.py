"""统一授权查询门（3.txt B.4/B.5）。

cancel、dispatch、inspect、resume、takeover、resolve capability 六个操作共用
这一个门：操作名合法 → run_id 是 opaque identifier → 目标任务存在 →
owner 一致性 → parent/delegation 可见性。每次操作过同一序列，杜绝
"有的入口校验、有的入口裸奔"的缺口。

R1 起：目标 run 在 owner runtime.db 有权威记录时，额外强制
WorkspaceBinding / TaskRun / parent+delegation / current AgentAttempt /
DB owner 五重校验（B.5）；无权威库或无权威记录（R1 前存量 run）保持
文件层防线（R0 现状）。完整伪造（同时篡改 task.json 与 runtime.db）超出
单层防线，归 R2 binding 与 OS 沙箱。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.opaque_id import OpaqueIdError, validate_opaque_id
from ..runtime_db.repository import BINDING_ACTIVE
from .models import SubAgentTask

OPERATIONS = frozenset(
    {"cancel", "dispatch", "inspect", "resume", "takeover", "resolve_capability"}
)
#: parent 链查询上限：防数据损坏成环时死循环；环 = 越权拒绝（fail-closed）。
PARENT_CHAIN_LIMIT = 8


class AuthorizationError(PermissionError):
    """授权门拒绝。语义与 load 的 FileNotFoundError 区分：存在但无权。"""


@dataclass(frozen=True)
class OperationRequest:
    """一次受门操作的请求上下文。

    requester_owner：请求方 owner 身份（agent.home_paths.owner_id /
    manager.owner_id），空 = 无 owner 权威信息（跳过 owner 一致性）。
    requester_run_id：请求方 run_id（子代理操作自己子树时），空 = 系统/
    主代理驱动（调度器、恢复编排、主代理工具循环），只做 owner 校验。
    """

    operation: str
    run_id: str
    requester_owner: str = ""
    requester_run_id: str = ""


def authorize_operation(
    manager: Any,
    request: OperationRequest,
    *,
    target: SubAgentTask | None = None,
) -> SubAgentTask:
    """统一授权查询门：校验通过返回已加载目标任务，失败抛 AuthorizationError。

    target 可预传（调用方已加载），省一次读盘；否则经 manager.load 加载——
    load 本身已含 run_id 拒绝式校验与 canonical state 重算。
    """
    operation = str(request.operation or "").strip()
    if operation not in OPERATIONS:
        raise AuthorizationError(f"未知操作: {operation!r}")
    # B.2/B.3：ID 拼路径前先过拒绝式校验（../、绝对路径、控制字符、超长）。
    try:
        run_id = validate_opaque_id(request.run_id, kind="run_id")
    except OpaqueIdError as exc:
        raise AuthorizationError(f"{operation}: 非法 run_id: {exc}") from exc
    if target is None:
        # 记录不存在保持 FileNotFoundError 透传，不转 AuthorizationError——
        # 后者只表示"存在但无权"。调用方需要区分两种失败：不存在走名册
        # 自纠/报告，无权才是授权拒绝（见 capability.py 的 FileNotFoundError
        # 分支）。六入口 catch 面均兼容（OSError 子类）。
        target = manager.load(run_id)
    _authorize_owner(target, request)
    _authorize_visibility(manager, target, request)
    _authorize_runtime_authority(manager, target, request)
    return target


def authorize_tree_scope(
    manager: Any,
    request: OperationRequest,
    root_id: str,
) -> None:
    """批量投影操作（inspect 的 root_id/status 扫描）的树级可见性门。

    请求方 run_id 非空时，目标树根必须与请求方同树（root_id 相等）——
    子代理只能看自己的树，不能扫兄弟树/父任务树。请求方无 run_id（主代理/
    系统）→ 放行。owner 一致性同样强制。
    """
    operation = str(request.operation or "").strip()
    if operation not in OPERATIONS:
        raise AuthorizationError(f"未知操作: {operation!r}")
    requester_run_id = str(request.requester_run_id or "").strip()
    if not requester_run_id:
        # 主代理/系统驱动，owner 由调用方保证（manager 边界即 owner 边界）。
        return
    if requester_run_id == str(root_id or "").strip():
        # 树根（主代理域，无 subagent 记录）看自己的树 → 放行。
        return
    try:
        requester = manager.load(requester_run_id)
    except FileNotFoundError as exc:
        raise AuthorizationError(
            f"{operation}: 请求方 run 不存在: {requester_run_id}"
        ) from exc
    requester_root = str(getattr(requester, "root_id", "") or requester_run_id)
    if requester_root != str(root_id or "").strip():
        raise AuthorizationError(
            f"{operation}: 目标树根 {root_id!r} 不在请求方子树内"
        )
    _authorize_owner(requester, request)
    _authorize_runtime_authority(manager, requester, request)


def _authorize_owner(task: SubAgentTask, request: OperationRequest) -> None:
    requester = str(request.requester_owner or "").strip()
    owner = str(getattr(task, "owner", "") or "").strip()
    if requester and owner and requester != owner:
        raise AuthorizationError(
            f"{request.operation}: 目标 owner={owner!r} 与请求方 owner={requester!r} 不一致"
        )


def _authorize_visibility(manager: Any, task: SubAgentTask, request: OperationRequest) -> None:
    requester_run_id = str(request.requester_run_id or "").strip()
    if not requester_run_id:
        return
    if str(getattr(task, "id", "") or "") == requester_run_id:
        return
    # 树根判据：requester 是目标树的根（主代理域，无 subagent 记录）→ 管理
    # 整棵树（含孤儿清理）。伪造 requester（非根 id）不满足此判据，走链校验。
    if requester_run_id == str(getattr(task, "root_id", "") or ""):
        return
    current_id = str(getattr(task, "parent_id", "") or "").strip()
    for _ in range(PARENT_CHAIN_LIMIT):
        if not current_id or current_id == requester_run_id:
            return
        ancestor = _load_ancestor(manager, current_id)
        if ancestor is None:
            # 链断（父记录不存在/被清理）且 requester 不是树根：无法证明目标
            # 在请求方子树内 → fail-closed 拒绝。孤儿任务归树根（主代理）域。
            break
        current_id = str(getattr(ancestor, "parent_id", "") or "").strip()
    raise AuthorizationError(
        f"{request.operation}: 目标不在请求方子树内（parent 链断/超 {PARENT_CHAIN_LIMIT} 层/成环）"
    )


def _authorize_runtime_authority(
    manager: Any,
    task: SubAgentTask,
    request: OperationRequest,
) -> None:
    """B.5：runtime.db 权威五重校验（TaskRun/Binding/parent+delegation/attempt/owner）。

    无权威库（无 owner home）或目标为 R1 前存量 run（无权威记录）→ 跳过，
    维持文件层防线；有权威记录则任一环缺失/失配 → fail-closed 拒绝。
    注意：task.json 与 DB 不一致时以 DB 为准（B.6：不信任文件自报值）。
    """
    repo = getattr(manager, "runtime_db", None)
    if repo is None:
        return
    run_id = str(getattr(task, "id", "") or "").strip()
    if not run_id:
        return
    agent_run = repo.agent_run_for_run_id(run_id)
    if agent_run is None:
        return  # R1 前存量 run：无权威记录，文件层防线兜底
    agent_run_id = str(agent_run["agent_run_id"] or "").strip()
    # 1. TaskRun 权威存在。
    task_run = repo.get_task_run(str(agent_run["task_run_id"] or "").strip())
    if task_run is None:
        raise AuthorizationError(
            f"{request.operation}: 权威 TaskRun 缺失: {run_id}"
        )
    # 2. owner 与 DB 权威一致（task.json 可被篡改，tasks.owner_id 才是权威）。
    db_task = repo.get_task(str(task_run["task_id"] or "").strip())
    requester_owner = str(request.requester_owner or "").strip()
    if db_task is not None:
        db_owner = str(db_task["owner_id"] or "").strip()
        if requester_owner and db_owner and requester_owner != db_owner:
            raise AuthorizationError(
                f"{request.operation}: DB 权威 owner={db_owner!r} 与请求方 "
                f"owner={requester_owner!r} 不一致"
            )
    # 3. 当前 AgentAttempt 必须存在（A.6/F.6：执行任何工具前必须有 attempt）。
    if repo.current_attempt(agent_run_id) is None:
        raise AuthorizationError(
            f"{request.operation}: 权威 current attempt 缺失: {run_id}"
        )
    # 4. parent/delegation：有 parent 的 child 必须有 immutable delegation 指向
    #    同 parent（A.7），缺失/失配即拒绝。
    parent_id = str(agent_run["parent_agent_run_id"] or "").strip()
    if parent_id:
        delegation = repo.delegation_for_child(agent_run_id)
        if delegation is None or str(delegation["parent_agent_run_id"] or "").strip() != parent_id:
            raise AuthorizationError(
                f"{request.operation}: 权威 delegation 缺失/失配: {run_id}"
            )
    # 5. WorkspaceBinding：建过 binding 的 run 必须仍是 ACTIVE（D.9 迁移后
    #    旧 run fail-closed）；从未建 binding（未执行）→ 放行。
    latest = repo.latest_binding_for_run(agent_run_id)
    if latest is not None and str(latest["status"] or "") != BINDING_ACTIVE:
        raise AuthorizationError(
            f"{request.operation}: WorkspaceBinding 非 ACTIVE: {run_id}"
        )


def _load_ancestor(manager: Any, run_id: str) -> SubAgentTask | None:
    # 只加载祖先本身（链查询），不再递归授权——可见性链查询是授权的一部分。
    try:
        return manager.load(run_id)
    except (FileNotFoundError, OSError):
        return None
