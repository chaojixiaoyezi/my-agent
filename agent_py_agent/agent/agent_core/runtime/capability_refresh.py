"""在原模型/工具安全点采用同attempt已提交的新授权，不改稳定提示前缀或重放工具。"""
from __future__ import annotations

import time
import weakref
from copy import copy
from dataclasses import replace

from ...path_access_policy import effective_owner_scope_root
from ...runtime_context import current_subagent_attempt_id, current_subagent_run_id
from ...subagents.capability_authority import task_tool_authority, valid_task_grants
from ...subagents.capability_updates import observe_capability_updates, subscribe_capability_updates
from ...subagents.services.runner_context_service import _runner_allowed_tools


class _ProviderHistoryOwners:
    """仅跟踪同请求刷新产生的活跃别名，不持正文或权限，也不登记任意replace视图。"""

    def __init__(self, identity, lock):
        self.identity = identity
        self.lock = lock
        self.views = weakref.WeakValueDictionary()

    def register(self, params):
        self.views[id(params)] = params

    def release(self, params):
        with self.lock:
            if self.views.get(id(params)) is not params:
                object.__setattr__(params, 'provider_history_messages', [])
                return
            for view in tuple(self.views.values()):
                object.__setattr__(view, 'provider_history_messages', [])


def release_compact_provider_history(params):
    """已决定摘要后解绑同请求能力刷新别名；独立视图沿原单对象解绑语义。"""
    with params._capability_refresh_lock:
        owners = params._provider_history_owners
        if owners is None:
            object.__setattr__(params, 'provider_history_messages', [])
        else:
            owners.release(params)


def refresh_subagent_capabilities(agent, params, *, tool_name=""):
    """通知只驱动发现；使用临时工具/路径的真实调用仍在原执行边界读canonical复核。"""
    run_id = current_subagent_run_id(agent)
    attempt = current_subagent_attempt_id(agent)
    if not run_id:
        return params
    if not attempt or run_id != params.run_id or attempt != params.attempt_id:
        raise PermissionError("CAPABILITY_REFRESH_IDENTITY_MISMATCH: runner与调用参数的run/attempt冲突；未采用新授权，请回到原执行轮核验身份，不要重放工具。")
    identity = (agent.home_paths.owner_id, run_id, attempt, params.request_id, params.task_id, params.context_scope)
    # 同步安全点内串行构造副本；异步通知只推进source epoch，不能修改已冻结的能力清单。
    with params._capability_refresh_lock:
        source = params._capability_update_source
        if source is not None and source.identity[3] != agent.home_paths.owner_id:
            raise PermissionError('CAPABILITY_REFRESH_OWNER_MISMATCH: canonical授权不属于当前宿主owner；未采用。')
        updated = _refresh_current_capabilities(agent, params, run_id, attempt, tool_name=tool_name)
        owners = params._provider_history_owners
        if owners is None or owners.identity != identity:
            owners = updated._provider_history_owners
        if owners is None or owners.identity != identity:
            owners = _ProviderHistoryOwners(identity, params._capability_refresh_lock)
        # 成功核验之后才绑定释放所有权；失败的异owner/attempt调用不能改动原执行对象。
        object.__setattr__(params, '_provider_history_owners', owners)
        object.__setattr__(updated, '_provider_history_owners', owners)
        owners.register(params)
        owners.register(updated)
        return updated


def _refresh_current_capabilities(agent, params, run_id, attempt, *, tool_name):
    """调用者持本请求锁；原canonical权限核验及不可变快照构造保持不变。"""
    source = params._capability_update_source
    if source is None or source.closed:
        source = subscribe_capability_updates(agent.subagents, run_id)
        params = replace(params, _capability_update_source=source, _capability_update_epoch=-1)
    if tool_name:
        # 先采用已通知epoch/已到期grant，再按缓存的依赖集合决定真实调用是否需要权威复核。
        params = refresh_subagent_capabilities(agent, params)
        if tool_name in params._capability_recheck_tools:
            observe_capability_updates(agent.subagents, agent.subagents.load(run_id))
            return refresh_subagent_capabilities(agent, params)
        return params
    epoch, task = source.current()
    now = time.time()
    if epoch == params._capability_update_epoch and (not params._capability_next_expiry or now < params._capability_next_expiry):
        return params
    if (task is None or task.runner_active_attempt_id != attempt or task.status != 'RUNNING'
            or task.ended_at):
        raise InterruptedError('CAPABILITY_REFRESH_SCOPE_CLOSED: 原owner/root/run/attempt或运行状态已变更，停止旧执行轮。')
    if source.identity[3] != agent.home_paths.owner_id:
        raise PermissionError('CAPABILITY_REFRESH_OWNER_MISMATCH: canonical授权不属于当前宿主owner；未采用。')
    previous = params.tool_runtime_snapshot
    grants = valid_task_grants(task, now=now)
    authority_tools, ambiguous = task_tool_authority(task, grants=grants)
    allowed = _runner_allowed_tools(task, authority_tools)
    # task授权只是上界；本轮正式关闭工具时，刷新不得把已冻结的空工具面重新打开。
    if not getattr(getattr(agent, 'config', None), 'enable_tools', True):
        allowed = []
    boundary_task = copy(task)
    boundary_task.capability_grants = grants
    boundary = agent.subagents.runner_context._build_write_boundary(boundary_task)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed, run_id=run_id,
        owner_scope_root=effective_owner_scope_root(agent, context_scope=params.context_scope, write_boundary=boundary))
    prior_order = {runtime.model_spec.name: i for i, runtime in enumerate(previous.runtimes)}
    runtimes = tuple(sorted(snapshot.runtimes, key=lambda runtime: prior_order.get(runtime.model_spec.name, len(prior_order))))
    snapshot = replace(snapshot, runtimes=runtimes, snapshot_hash="")
    # 保留已有展示策略与加载顺序；增量工具仍走原发现入口，不重排system目录。
    keep = snapshot.available_tool_names
    snapshot = replace(snapshot,
        loaded_tool_names=tuple(n for n in previous.loaded_tool_names if n in keep),
        preloaded_tool_names=tuple(n for n in previous.preloaded_tool_names if n in keep),
        presentation_deferred_names=(previous.presentation_deferred_names & keep
            if previous.presentation_deferred_names is not None else None),
        presentation_shortlist_names=(previous.presentation_shortlist_names & keep
            if previous.presentation_shortlist_names is not None else None))
    independent, _ = task_tool_authority(task, grants=[])
    temporary = keep - set(_runner_allowed_tools(task, independent))
    recheck = temporary | ({r.model_spec.name for r in snapshot.runtimes if _may_use_path_scope(r)}
        if any(g.path_scope for g in grants) else set())
    if ambiguous:
        params.tool_context.append("[capability-authority] LEGACY_CAPABILITY_BASE_UNKNOWN: "
            + ", ".join(ambiguous[:16]) + f"（共{len(ambiguous)}项）"
            + "；旧账未保存独立权限来源，未恢复已过期授权。请用capability_request为当前run重新申请列出的工具；其余请直属父核对原grant。不要原样重试受拒调用。")
    return replace(params, allowed_tools=allowed, write_boundary=boundary, tool_runtime_snapshot=snapshot,
        _capability_update_epoch=epoch,
        _capability_next_expiry=min((g.expires_at for g in grants if g.expires_at), default=0.0),
        _capability_recheck_tools=frozenset(recheck))


def _may_use_path_scope(runtime):
    policy = runtime.runtime_policy.resource_scopes
    if policy.mode == 'none':
        return False
    if policy.mode == 'declared':
        return any(policy.parameter_kinds.get(name, 'path') == 'path' for name in policy.parameter_names) or any(
            scope.startswith('workspace:') for scope in policy.static_scopes)
    # 参数来源未完整声明时，不猜成无路径；仅活跃path grant期间保守复核。
    return True
