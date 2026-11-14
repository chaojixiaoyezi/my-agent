"""当前 attempt 的授权变更通知；canonical 是权威，弱订阅只避免热路径重复读盘。"""
from __future__ import annotations

import copy
import threading
import weakref

_LOCK = threading.Lock()
_SUBSCRIBERS = weakref.WeakValueDictionary()


def _key(manager, run_id):
    return str(manager.workspace), run_id


def _identity(task):
    identity = task.runtime_identity
    return (task.id, task.parent_id, task.root_id, identity.service_owner_id,
            identity.effective_principal_id, task.runner_active_attempt_id)


def capability_authority_fields(task):
    # 仅canonical原写入路径比较；读心跳只看持久capability_revision，不遍历grant历史。
    return (task.base_allowed_tools, task.allowed_tools, task.effective_permissions,
            task.capability_grants, [(r.id, r.from_run_id, r.status) for r in task.capability_requests],
            task.allowed_write_roots, task.forbidden_write_roots, task.context_manifest,
            (task.attributes or {}).get('delegation_constraints'))


class _SubscriptionHub:
    def __init__(self):
        self.sources = weakref.WeakValueDictionary()


def _authority_view(task):
    # 不深拷贝goal/result/历史/产物；仅在授权revision变化时复制工具与路径权限的可变字段。
    value = copy.copy(task)
    for name in ('allowed_tools', 'base_allowed_tools', 'effective_permissions', 'capability_grants',
                 'capability_requests', 'allowed_write_roots', 'forbidden_write_roots', 'locked_files',
                 'runtime_identity', 'context_manifest', 'attributes'):
        setattr(value, name, copy.deepcopy(getattr(task, name)))
    return value


class CapabilityUpdateSubscription:
    def __init__(self, task, hub):
        self.hub = hub
        self.identity = _identity(task)
        self.revision = -1
        self.epoch = 0
        self.closed = False
        self.task = None
        self.authority = None
        self.lock = threading.Lock()
        self.observe(task)

    def observe(self, task):
        with self.lock:
            if task.state_revision <= self.revision:
                return
            self.revision = task.state_revision
            # 切attempt、改owner/root或进入终态都不能复用旧执行的授权。
            identity = _identity(task)
            facts = (task.capability_revision, identity, task.status, bool(task.ended_at))
            if facts == self.authority:
                return
            self.authority = facts
            self.task = _authority_view(task) if identity == self.identity else None
            self.epoch += 1

    def current(self):
        with self.lock:
            return self.epoch, self.task


def subscribe_capability_updates(manager, run_id):
    # 新attempt首次绑定两次读原canonical，封闭建立订阅窗口；同attempt重订阅复用原源。
    task = manager.load(run_id)
    key = _key(manager, run_id)
    with _LOCK:
        hub = _SUBSCRIBERS.get(key)
        if hub is None:
            hub = _SubscriptionHub()
            _SUBSCRIBERS[key] = hub
        source = hub.sources.get(task.runner_active_attempt_id)
        if source is None:
            source = CapabilityUpdateSubscription(task, hub)
            hub.sources[task.runner_active_attempt_id] = source
    source.observe(manager.load(run_id))
    return source


def observe_capability_updates(manager, task):
    with _LOCK:
        hub = _SUBSCRIBERS.get(_key(manager, task.id))
        sources = tuple(hub.sources.values()) if hub is not None else ()
    # 仅当前run仍活的attempt源；新attempt不遮蔽旧源的撤权/取消通知，弱引用归零即释放。
    for source in sources:
        source.observe(task)


def release_capability_updates(source):
    if source is None:
        return
    source.closed = True
    with _LOCK:
        attempt = source.identity[-1]
        if source.hub.sources.get(attempt) is source:
            source.hub.sources.pop(attempt, None)
