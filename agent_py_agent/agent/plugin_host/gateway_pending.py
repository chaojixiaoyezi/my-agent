# LLM: 待办只取原审批/私卡账和当前claim，签名不是批准；原字段、秘密和ref不进入公开结果。
# 模块用途: 为生产交互域冷读取原待办，并把内部真人票据交还原提交服务。
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict, deque
from functools import partial
from types import SimpleNamespace

from ..conversation.input_dependencies import InputDependencyAccess
from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
from ..subagents.services.persistence.service import load_subagent_task
from ..user_space.interaction_refs import verify_interaction_reference
from .contracts import resolve_read_context
from .errors import HostAPIError
from .gateway_runtime import runtime_rows
from .interactions import approval_interaction, respond_approval
from .schemas import thaw


# LLM: 只能借用已存在且owner完全相同的Agent来核其活动claim，不触发pool创建，不继承其执行器。
# 函数用途: 找到原审批所需的当前已加载宿主。
def loaded_owner_agent(services, context):
    agent = services.resources.server.agent
    home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    return agent if home == context.owner_home.home_dir else None


# LLM: 冷输入读取只使用原Store和子任务reader；没有quota/upsert，因此这个对象不能用来写。
# 函数用途: 建立原输入依赖的只读访问口。
def read_inputs(services, context):
    paths = services.resources.runtime_paths(context)
    return InputDependencyAccess(services.resources.store_for(context), context.identity, None,
                                partial(load_subagent_task, paths["subagent_workspace"]), None)


# LLM: MAC、原owner和原输入current都需通过；过期卡不是活待办，坏签名不泄漏其原引用。
# 函数用途: 从原私卡有界读取当前线程的安全内部交互投影。
def pending_inputs(services, authorized, context, thread_id):
    import time

    from ..gateway_parts.private_interaction_service import read_plugin_pending_input
    home = context.owner_home
    rows = runtime_rows(home, home.home_dir.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS))
    inputs, result = read_inputs(services, context), []
    for row in rows:
        kind = row.get("kind")
        if kind not in {"task_credential", "model_configuration", "feishu_oauth"} or row.get("expires_at", 0) <= time.time():
            continue
        reference = row.get("reference") or row.get("state")
        try:
            payload = verify_interaction_reference(home.root, reference, channel=row["channel"], actor_id=row["actor_id"])
        except (ValueError, KeyError, TypeError):
            raise HostAPIError("STORE_UNAVAILABLE", 503) from None
        presentation = "presentation-" + hashlib.sha256(reference.encode()).hexdigest()[:40]
        item = read_plugin_pending_input(home.home_dir, inputs, payload, presentation_ref=presentation)
        if item is not None and item["thread_id"] == thread_id:
            result.append({**item, "payload": payload})
    return result


_PENDING_DISCOVERY_CACHE_LIMIT_COUNT = 64
_PENDING_DISCOVERY_CACHE_IDLE_SECONDS = 60.0
_pending_cache_clock = time.monotonic
_current_approval_roots = OrderedDict()
_current_approval_roots_lock = threading.RLock()


def _run_membership_stamp(index):
    revision = index.current_membership_revision()
    info = index.path.stat(follow_symlinks=False)
    return info.st_dev, info.st_ino, revision


def _current_child_approval_roots(workspace, store, thread_id, *, deadline, scan_limit=128):
    """复用原status分页索引；跨poll推进，源变化使整个当前集合发现失效。"""
    from pathlib import Path

    from ..subagents.models import SUBAGENT_ENDED_STATUSES, SUBAGENT_TASK_STATUSES
    from ..subagents.services.persistence.run_index import SubagentRunIndex

    workspace = Path(workspace)
    if not workspace.exists():
        return set(), True
    index = SubagentRunIndex(workspace, read_only=True)
    key = str(workspace), thread_id
    if not _current_approval_roots_lock.acquire(blocking=False):
        return set(), False
    state = None
    try:
        touched = _pending_cache_clock()
        for old, value in list(_current_approval_roots.items()):
            if not value['in_use'] and touched - value['touched'] >= _PENDING_DISCOVERY_CACHE_IDLE_SECONDS:
                _current_approval_roots.pop(old)
        stamp = _run_membership_stamp(index)
        state = _current_approval_roots.get(key)
        if state is None or state['stamp'] != stamp:
            if state is None and len(_current_approval_roots) >= _PENDING_DISCOVERY_CACHE_LIMIT_COUNT:
                retired = next((old for old, value in _current_approval_roots.items() if value['complete']), None)
                if retired is None:
                    return set(), False
                _current_approval_roots.pop(retired)
            state = {'stamp': stamp, 'after': '', 'roots': set(), 'root_threads': {}, 'complete': False,
                     'touched': touched, 'in_use': False}
            _current_approval_roots[key] = state
        _current_approval_roots.move_to_end(key)
        state.update(touched=touched, in_use=True)
        # 完整缓存也须走原index覆盖/pending门；只保查找refs，不借缓存绕过canonical提交。
        if state['complete']:
            readiness = index.readiness()
            if readiness.get('complete') is not True or readiness.get('pending'):
                raise HostAPIError('STORE_UNAVAILABLE', 503)
        else:
            statuses = tuple(sorted(SUBAGENT_TASK_STATUSES - SUBAGENT_ENDED_STATUSES))
            ids, after = index.candidate_ids(statuses=statuses, after=state['after'], limit=scan_limit)
            for position, run_id in enumerate(ids):
                if time.monotonic() >= deadline:
                    state['after'] = ids[position - 1] if position else state['after']
                    return set(), False
                task = load_subagent_task(workspace, run_id)
                if task is None:
                    raise HostAPIError('STORE_UNAVAILABLE', 503)
                if task.status in SUBAGENT_ENDED_STATUSES:
                    raise HostAPIError('STORE_UNAVAILABLE', 503)
                root = str(task.root_id or '')
                if root not in state['root_threads']:
                    link, error = store.tasks.load_report(root)
                    if error is not None or link is None:
                        raise HostAPIError('STORE_UNAVAILABLE', 503)
                    state['root_threads'][root] = link.thread_id
                if state['root_threads'][root] == thread_id:
                    state['roots'].add(root)
                state['after'] = run_id
            state['after'] = after
            state['complete'] = not after
        if stamp != _run_membership_stamp(index):
            _current_approval_roots.pop(key, None)
            return set(), False
        return set(state['roots']) if state['complete'] else set(), state['complete']
    finally:
        if state is not None:
            state.update(touched=_pending_cache_clock(), in_use=False)
        # pending/忙只是当前不可用；保留游标，下一拍先核持久成员代次再决定能否续用。
        _current_approval_roots_lock.release()


_approval_root_rounds = OrderedDict()
_approval_root_rounds_lock = threading.RLock()


def _approval_round(store, thread_id, roots):
    key = str(store.storage.root), thread_id
    with _approval_root_rounds_lock:
        touched = _pending_cache_clock()
        for old, value in list(_approval_root_rounds.items()):
            if touched - value['touched'] < _PENDING_DISCOVERY_CACHE_IDLE_SECONDS:
                continue
            if value['lock'].acquire(blocking=False):
                try:
                    _approval_root_rounds.pop(old)
                finally:
                    value['lock'].release()
        state = _approval_root_rounds.get(key)
        if state is None or state['roots'] != roots:
            if state is None and len(_approval_root_rounds) >= _PENDING_DISCOVERY_CACHE_LIMIT_COUNT:
                # 不逐出未完成轮次，保住已开始owner的公平进展；完成轮次可随时冷重建。
                retired = next((old for old, value in _approval_root_rounds.items() if not value['queue'] and not value['lock'].locked()), None)
                if retired is None:
                    raise HostAPIError('STORE_UNAVAILABLE', 503)
                _approval_root_rounds.pop(retired)
            state = {'roots': roots, 'queue': deque(roots), 'refs': {}, 'stamps': {}, 'lock': threading.Lock(),
                     'touched': touched}
            _approval_root_rounds[key] = state
        _approval_root_rounds.move_to_end(key)
        state['touched'] = touched
        return state


def _pending_approval_round(agent, store, thread_id, roots, *, deadline):
    from ..conversation.agent_tool_approval import (
        _approval_directory_stamp,
        _approval_root_for_store,
        _strict_pending_approval,
        list_pending_agent_tool_approvals_report,
    )

    state = _approval_round(store, thread_id, roots)
    if not state['lock'].acquire(blocking=False):
        raise HostAPIError('STORE_UNAVAILABLE', 503)
    try:
        remaining, visited = 128, 0
        while state['queue'] and remaining > 0 and visited < 128 and time.monotonic() < deadline:
            root = state['queue'][0]
            progress = {}
            _rows, complete = list_pending_agent_tool_approvals_report(
                agent, root_task_id=root, deadline=deadline, scan_limit=remaining, scan_progress=progress)
            remaining -= progress.get('entries', 0)
            if not complete:
                break
            state['refs'][root] = progress['references']
            state['stamps'][root] = progress['source_stamp']
            state['queue'].popleft()
            visited += 1
        if state['queue']:
            raise HostAPIError('STORE_UNAVAILABLE', 503)
        changed = [root for root in roots if state['stamps'][root] != _approval_directory_stamp(_approval_root_for_store(store, root))]
        if changed:
            state['queue'].extend(changed)
            raise HostAPIError('STORE_UNAVAILABLE', 503)
        # 输出工作只随当前pending数量增长；扫描历史的128预算不截断owner的有效结果。
        rows = []
        for root in roots:
            for path in state['refs'][root]:
                row = _strict_pending_approval(agent, root, path)
                if row is not None:
                    rows.append(row)
        if any(state['stamps'][root] != _approval_directory_stamp(_approval_root_for_store(store, root)) for root in roots):
            state['queue'].extend(root for root in roots if root not in state['queue'])
            raise HostAPIError('STORE_UNAVAILABLE', 503)
        return rows
    finally:
        state['touched'] = _pending_cache_clock()
        state['lock'].release()


# LLM: 原TaskLink确定审批根；无加载主体且确有审批记录时无法核当前claim，应明确不可用而非伪空。
# 函数用途: 汇合准确线程的原审批和原输入元数据。
def pending_interactions(services, authorized, thread_id):
    from ..conversation.task_promotion import conversation_thread_execution_state

    context = resolve_read_context(authorized)
    store = services.resources.store_for(context)
    deadline = time.monotonic() + 1.5
    try:
        tasks, errors = store.tasks.active_report(thread_id)
        execution = conversation_thread_execution_state(store, thread_id)
        if errors or execution.get('state_available') is not True:
            raise HostAPIError('STORE_UNAVAILABLE', 503)
        roots = {task.task_id for task in tasks}
        for task_id in execution.get('running_task_ids') or []:
            link, error = store.tasks.link_report(thread_id, task_id)
            if error is not None:
                raise HostAPIError('STORE_UNAVAILABLE', 503)
            if link is not None:
                roots.add(link.task_id)
        paths = services.resources.runtime_paths(context)
        children, complete = _current_child_approval_roots(paths['subagent_workspace'], store, thread_id, deadline=deadline)
        if not complete:
            raise HostAPIError('STORE_UNAVAILABLE', 503)
        roots.update(children)
        result = pending_inputs(services, authorized, context, thread_id)
        agent = loaded_owner_agent(services, context)
        permitted = []
        for root in sorted(roots):
            link, error = store.tasks.link_report(thread_id, root)
            if error is not None or link is None:
                raise HostAPIError('STORE_UNAVAILABLE', 503)
            if services.allows(authorized, 'task', root):
                permitted.append(root)
        if agent is None:
            from ..common.nofollow_fs import open_directory_beneath
            for root in permitted:
                try:
                    descriptor = open_directory_beneath(store.storage.root, ('subagent_tool_approvals', root))
                except FileNotFoundError:
                    continue
                try:
                    with os.scandir(descriptor) as entries:
                        if next(entries, None) is not None:
                            raise HostAPIError('HOST_NOT_READY', 503)
                finally:
                    os.close(descriptor)
            return result
        pending = _pending_approval_round(agent, store, thread_id, tuple(permitted), deadline=deadline)
        for item in pending:
            reference = 'approval-' + hashlib.sha256(str(item['request']['permission_id']).encode()).hexdigest()[:40]
            result.append(approval_interaction(agent, item, reference))
        return result
    except HostAPIError:
        raise
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        raise HostAPIError('STORE_UNAVAILABLE', 503) from None


# LLM: 公开插件调用不能直接进入本口；只有ResponseTickets的已认证冻结HumanResponse可到这里。
# 函数用途: 提交当前owner原审批或私卡的准确真人决定。
def respond_interaction(services, authorized, ticket):
    context = resolve_read_context(authorized)
    agent = loaded_owner_agent(services, context)
    if agent is None:
        raise HostAPIError("HOST_NOT_READY", 503)
    if ticket.interaction["kind"] == "tool_approval":
        return respond_approval(services, agent, authorized, ticket)
    from ..conversation.input_dependencies import input_dependency_owner_resources
    from ..gateway_parts.private_interaction_service import execute_plugin_input
    from ..user_space.owner_resolver import home_paths_with_owner
    resources = services.resources
    paths = resources.runtime_paths(context)
    inputs, repo = input_dependency_owner_resources(resources.store_for(context), context.identity,
        owner_home=context.owner_home.home_dir, subagent_workspace=paths["subagent_workspace"], commit=None, blocking=True)
    from ..capability.runtime_config_reload import deployment_capability_config
    native = SimpleNamespace(base=agent, inputs=inputs, home_paths=home_paths_with_owner(agent.home_paths, context.owner_home),
        capability=deployment_capability_config(agent.home_paths))

    # LLM: guard由原卡锁内调用，不能在提交前假装CAS已经通过。
    # 函数用途: 在原写门内再次核当前来源及原卡版本。
    def guard(row):
        from .interactions import approval_revision
        services.revalidate(authorized)
        if approval_revision(row) != ticket.revision:
            raise HostAPIError("REVISION_CONFLICT", 409)

    try:
        execute_plugin_input(native, thaw(ticket.interaction["payload"]), thaw(ticket.decision_payload),
                             thaw(ticket.human_identity), guard)
        return "cancelled" if ticket.decision_payload.get("decision") == "cancel" else "submitted"
    finally:
        repo.close()
