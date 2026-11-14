"""V06-07 回归：退休前的人工审批义务必须真的被读到，未知覆盖不得放行退休。

修复点（agent/conversation/background_team_lifecycle.py）：
- 审批读口 `list_pending_agent_tool_approvals` 只接受 root_task_id，原实现多传 thread_id 触发 TypeError；
- 原实现 deadline=0 让预算扫描直接跳过，等于从不查审批；
- 原实现 `except Exception: pass` 把读失败当成"没有审批"，于是 retiring 错误地变成 retired。

本测试只替换宿主 store 读口与审批读口，production 的 identity_outstanding 与
_reconcile_background_identity_locked 原样执行；不启 Gateway、不跑模型。
"""
import inspect
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import agent_tool_approval
from agent_py_agent.agent.conversation.background_team_lifecycle import (
    _reconcile_background_identity_locked,
    identity_outstanding,
)
from agent_py_agent.agent.conversation.models import ConversationThread


def _host(*, active_roots=()):
    thread = ConversationThread(thread_id='worker', owner_id='local/test',
        canonical_user_id='local/test', metadata={'background_agent': {
            'agent_id': 'worker', 'managed_by_thread_id': 'controller',
            'space_id': None, 'project_ref': None, 'identity_state': 'retiring',
            'after_task': 'reuse', 'identity_revision': 1}})

    class Threads:
        current = thread

        def require(self, _thread_id):
            return self.current

        def update_atomic(self, _thread_id, update):
            self.current = update(self.current)
            return self.current

    links = [SimpleNamespace(thread_id='worker', task_id=root) for root in active_roots]
    store = SimpleNamespace(
        threads=Threads(),
        session_tasks=SimpleNamespace(list_page=lambda **kw: {
            'items': [], 'coverage': 'complete', 'load_errors': [], 'next_cursor': None}),
        tasks=SimpleNamespace(active_report=lambda _tid: (list(links), [])),
        claims=SimpleNamespace(load_report=lambda _tid: ({}, None)),
        wakes=SimpleNamespace(pending_report=lambda **kw: ([], [])),
        progress=SimpleNamespace(list_report=lambda **kw: ([], [])),
        guidance=SimpleNamespace(pending_report=lambda *args, **kw: ([], [])),
    )
    return SimpleNamespace(conversation_store=store,
        business_spaces_catalog=SimpleNamespace(_read=lambda: {'subscriptions': {}}))


def _patch_reader(monkeypatch, *, rows=(), error=None, complete=True, seen=None):
    def reader(*args, **kwargs):
        if seen is not None:
            seen.append(kwargs)
        progress = kwargs.get('scan_progress')
        if isinstance(progress, dict):
            progress['entries'] = len(rows)
            if complete:
                progress['complete'] = True
        if error is not None:
            raise error
        return list(rows), complete

    monkeypatch.setattr(agent_tool_approval, 'list_pending_agent_tool_approvals_report', reader)


def test_reader_signature_has_no_thread_id():
    # 读口合同：只有 root_task_id；再传 thread_id 会 TypeError，不能靠吞异常掩盖。
    for fn in (
        agent_tool_approval.list_pending_agent_tool_approvals,
        agent_tool_approval.list_pending_agent_tool_approvals_report,
    ):
        params = inspect.signature(fn).parameters
        assert 'root_task_id' in params and 'thread_id' not in params


def test_empty_complete_approval_scan_releases(monkeypatch):
    active_host = _host(active_roots=('root-work',))
    seen = []
    _patch_reader(monkeypatch, rows=(), complete=True, seen=seen)
    active_refs, _ = identity_outstanding(active_host, active_host.conversation_store.threads.require('worker'))
    assert seen and 'thread_id' not in seen[0] and seen[0].get('root_task_id') == 'root-work'
    assert [ref for ref in active_refs if str(ref).startswith('approval')] == []

    # active TaskLink 清空后不拿 thread_id 充当 root_task_id，完整无未决义务时放行 retired。
    idle_host = _host()
    idle_seen = []
    _patch_reader(monkeypatch, rows=(), complete=True, seen=idle_seen)
    before = idle_host.conversation_store.threads.require('worker')
    refs, _ = identity_outstanding(idle_host, before)
    actual = _reconcile_background_identity_locked(idle_host, 'worker')
    state = actual.metadata['background_agent']['identity_state']
    assert idle_seen == []
    assert refs == []
    assert state == 'retired'


def test_pending_approval_keeps_retiring(monkeypatch):
    host = _host(active_roots=('root-work',))
    _patch_reader(monkeypatch, rows=[{'request': {'permission_id': 'pending-approval'}}], complete=True)
    before = host.conversation_store.threads.require('worker')
    refs, _ = identity_outstanding(host, before)
    actual = _reconcile_background_identity_locked(host, 'worker')
    state = actual.metadata['background_agent']['identity_state']
    assert 'approval:pending-approval' in refs
    assert state == 'retiring'


def test_unreadable_approval_store_keeps_retiring(monkeypatch):
    host = _host(active_roots=('root-work',))
    _patch_reader(monkeypatch, error=OSError('controlled approval read failure'), complete=False)
    before = host.conversation_store.threads.require('worker')
    refs, _ = identity_outstanding(host, before)
    actual = _reconcile_background_identity_locked(host, 'worker')
    state = actual.metadata['background_agent']['identity_state']
    assert 'approvals:unreadable' in refs
    assert state == 'retiring'


def test_incomplete_approval_scan_keeps_retiring(monkeypatch):
    host = _host(active_roots=('root-work',))
    _patch_reader(monkeypatch, rows=[], complete=False)
    before = host.conversation_store.threads.require('worker')
    refs, _ = identity_outstanding(host, before)
    actual = _reconcile_background_identity_locked(host, 'worker')
    state = actual.metadata['background_agent']['identity_state']
    assert 'approvals:incomplete' in refs
    assert state == 'retiring'
