"""待审批发现只读当前成员及原canonical scope，不受历史库存或心跳写打断。"""
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import agent_tool_approval as approvals
from agent_py_agent.agent.plugin_host import gateway_pending
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.subagents.models import SubAgentTask
from agent_py_agent.agent.subagents.services.persistence.run_index import (
    SubagentRunIndex,
    SubagentRunIndexUnavailable,
)
from agent_py_agent.tests.test_gateway_agent_control_service import (
    _bound_agent_tree,
    _child_approval_request,
)


def _current_set(tmp_path, count):
    workspace = tmp_path / 'runs'
    workspace.mkdir()
    index = SubagentRunIndex(workspace)
    store = ConversationStore(tmp_path / 'conversation')
    thread = store.threads.get_or_create({'canonical_user_id': 'user', 'channel': 'chat', 'channel_conversation_id': 'one', 'channel_user_id': 'user'})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'root', 'status': 'active', 'goal': 'root'})
    tasks = []
    with index.connect() as db:
        for number in range(count):
            task = SubAgentTask(id=f'run-{number:04d}', root_id='root', parent_id='root', goal='work', thought='', plan=[], status='RUNNING')
            directory = workspace / task.id
            directory.mkdir()
            (directory / 'task.json').write_text(json.dumps(asdict(task)))
            index.upsert(task, connection=db)
            tasks.append(task)
    return workspace, index, store, thread, tasks


@pytest.mark.parametrize('count', [129, 300])
def test_current_roots_finish_across_polls_during_continuous_metadata_writes(tmp_path, monkeypatch, count):
    workspace, index, store, thread, tasks = _current_set(tmp_path, count)
    revision = index.current_membership_revision()
    calls = []
    original = gateway_pending.load_subagent_task
    def load(*args):
        calls.append(args[1])
        return original(*args)
    monkeypatch.setattr(gateway_pending, 'load_subagent_task', load)
    for iteration in range(8):
        before = len(calls)
        roots, complete = gateway_pending._current_child_approval_roots(workspace, store, thread.thread_id, deadline=time.monotonic() + 10)
        assert len(calls) - before <= 128
        if complete:
            assert roots == {'root'}
            break
        # 真实canonical和索引普通内容更新，成员和root不变；不应让续读永远从头开始。
        with index.connect() as db:
            for task in tasks:
                updated = replace(task, updated_at=time.time(), goal=f'heartbeat-{iteration}')
                (workspace / task.id / 'task.json').write_text(json.dumps(asdict(updated)))
                index.upsert(updated, connection=db)
        assert index.current_membership_revision() == revision
    else:
        pytest.fail('当前集合必须跨poll最终覆盖，128不是能力上限')
    assert len(calls) == count
    assert SubagentRunIndex(workspace, read_only=True).current_membership_revision() == revision


def test_true_membership_change_invalidates_current_cursor_and_cached_complete(tmp_path):
    workspace, index, store, thread, tasks = _current_set(tmp_path, 129)
    roots, complete = gateway_pending._current_child_approval_roots(workspace, store, thread.thread_id, deadline=time.monotonic() + 10)
    assert not complete
    before = index.current_membership_revision()
    changed = replace(tasks[0], status='DONE')
    (workspace / changed.id / 'task.json').write_text(json.dumps(asdict(changed)))
    index.upsert(changed)
    assert index.current_membership_revision() != before
    roots, complete = gateway_pending._current_child_approval_roots(workspace, store, thread.thread_id, deadline=time.monotonic() + 10)
    assert complete and roots == {'root'}
    inserted = replace(tasks[0], id='run-new')
    (workspace / inserted.id).mkdir()
    (workspace / inserted.id / 'task.json').write_text(json.dumps(asdict(inserted)))
    index.upsert(inserted)
    _, complete = gateway_pending._current_child_approval_roots(workspace, store, thread.thread_id, deadline=time.monotonic() + 10)
    assert not complete


def test_missing_membership_generation_is_unknown_until_original_writer_installs_it(tmp_path):
    workspace, index, _, _, _ = _current_set(tmp_path, 1)
    with index.connect() as db:
        db.execute("DELETE FROM meta WHERE key='current_membership_revision'")
    with pytest.raises(SubagentRunIndexUnavailable):
        SubagentRunIndex(workspace, read_only=True).current_membership_revision()
    installed = SubagentRunIndex(workspace)
    assert installed.current_membership_revision() > 0


def _approval_tree(tmp_path):
    agent, _, child = _bound_agent_tree(tmp_path)
    handle = approvals.publish_agent_tool_approval(agent, run_id=child.id, thread_id=child.agent_thread_id,
                                                   request_value=_child_approval_request(child.id))
    return agent, child, handle


def test_approval_report_scans_history_in_slices_then_revalidates_and_invalidates(tmp_path):
    agent, child, handle = _approval_tree(tmp_path)
    directory = handle.path.parent
    # 目录顺序不是选择权威；将真实pending放在终态库存之后。
    pending = handle.path.read_bytes()
    handle.path.unlink()
    for number in range(300):
        (directory / f'history-{number:04d}.json').write_text('{"status":"approved"}')
    handle.path.write_bytes(pending)
    for _ in range(8):
        rows, complete = approvals.list_pending_agent_tool_approvals_report(agent, root_task_id=child.root_id,
            deadline=time.monotonic() + 10, scan_limit=128)
        if complete:
            break
        assert rows == []
    assert complete and len(rows) == 1
    handle.path.write_text('{broken')
    with pytest.raises(approvals.AgentToolApprovalDiscoveryUnavailable):
        for _ in range(8):
            approvals.list_pending_agent_tool_approvals_report(agent, root_task_id=child.root_id,
                deadline=time.monotonic() + 10, scan_limit=128)


def test_gateway_finds_child_pending_under_completed_parent_and_rechecks_permissions(tmp_path, monkeypatch):
    agent, child, handle = _approval_tree(tmp_path)
    store = agent.conversation_store
    root = store.tasks.load(child.root_id)
    store.tasks.update_status({'task_id': root.task_id, 'status': 'completed', 'expected_status': 'active'})
    context = SimpleNamespace(owner_home=SimpleNamespace(home_dir=agent.home_paths.owner_home_dir))
    resources = SimpleNamespace(server=SimpleNamespace(agent=agent), store_for=lambda _: store,
        runtime_paths=lambda _: {'subagent_workspace': agent.subagents.workspace})
    permission = [True]
    checks = []
    def allows(auth, kind, identifier):
        checks.append((kind, identifier))
        return permission[0]
    services = SimpleNamespace(resources=resources, allows=allows)
    monkeypatch.setattr(gateway_pending, 'resolve_read_context', lambda _: context)
    monkeypatch.setattr(gateway_pending, 'pending_inputs', lambda *args: [])
    monkeypatch.setattr(store.tasks, 'list_report', lambda *a, **k: pytest.fail('不能读取TaskStore全史'))
    rows = gateway_pending.pending_interactions(services, object(), root.thread_id)
    assert len(rows) == 1 and rows[0]['kind'] == 'tool_approval'
    assert ('task', root.task_id) in checks
    permission[0] = False
    assert gateway_pending.pending_interactions(services, object(), root.thread_id) == []
    assert handle.path.read_bytes()


def test_current_root_association_change_and_cold_revision_are_structural(tmp_path):
    import subprocess
    import sys

    workspace, index, store, thread, tasks = _current_set(tmp_path, 1)
    roots, complete = gateway_pending._current_child_approval_roots(workspace, store, thread.thread_id, deadline=time.monotonic() + 10)
    assert complete and roots == {'root'}
    before = index.current_membership_revision()
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'new-root', 'status': 'active', 'goal': 'new'})
    changed = replace(tasks[0], root_id='new-root', parent_id='new-root')
    (workspace / changed.id / 'task.json').write_text(json.dumps(asdict(changed)))
    index.upsert(changed)
    after = index.current_membership_revision()
    assert after != before
    roots, complete = gateway_pending._current_child_approval_roots(workspace, store, thread.thread_id, deadline=time.monotonic() + 10)
    assert complete and roots == {'new-root'}
    code = ('import sys; from pathlib import Path; '
            'from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex; '
            'print(SubagentRunIndex(Path(sys.argv[1]),read_only=True).current_membership_revision())')
    assert int(subprocess.check_output([sys.executable, '-c', code, str(workspace)], text=True)) == after


def test_live_unknown_scope_is_not_empty_and_expired_scope_needs_no_revival(tmp_path):
    agent, child, handle = _approval_tree(tmp_path)
    row = json.loads(handle.path.read_text())
    (agent.subagents.workspace / child.id / 'task.json').write_text('{bad')
    with pytest.raises(approvals.AgentToolApprovalDiscoveryUnavailable):
        approvals.list_pending_agent_tool_approvals_report(agent, root_task_id=child.root_id, deadline=time.monotonic() + 10)
    row['expires_at'] = 1
    handle.path.write_text(json.dumps(row))
    rows, complete = approvals.list_pending_agent_tool_approvals_report(agent, root_task_id=child.root_id, deadline=time.monotonic() + 10)
    assert complete and rows == []


def test_approval_new_pending_directory_generation_invalidates_complete_cache(tmp_path):
    agent, child, handle = _approval_tree(tmp_path)
    rows, complete = approvals.list_pending_agent_tool_approvals_report(agent, root_task_id=child.root_id, deadline=time.monotonic() + 10)
    assert complete and len(rows) == 1
    second = approvals.publish_agent_tool_approval(agent, run_id=child.id, thread_id=child.agent_thread_id,
        request_value=_child_approval_request(child.id, request_id='second-request'))
    rows, complete = approvals.list_pending_agent_tool_approvals_report(agent, root_task_id=child.root_id, deadline=time.monotonic() + 10)
    assert complete and len(rows) == 2
    assert handle.path != second.path


def test_three_hundred_distinct_completed_roots_finish_without_total_pending_cap(tmp_path, monkeypatch):
    workspace, index, store, thread, tasks = _current_set(tmp_path, 300)
    with index.connect() as db:
        for number, task in enumerate(tasks):
            root_id = f'root-{number:04d}'
            store.tasks.bind({'thread_id': thread.thread_id, 'task_id': root_id, 'status': 'completed', 'goal': 'closed parent'})
            changed = replace(task, root_id=root_id, parent_id=root_id)
            (workspace / task.id / 'task.json').write_text(json.dumps(asdict(changed)))
            index.upsert(changed, connection=db)
    owner_home = tmp_path / 'owner'
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_home_dir=owner_home), conversation_store=store)
    context = SimpleNamespace(owner_home=SimpleNamespace(home_dir=owner_home))
    resources = SimpleNamespace(server=SimpleNamespace(agent=agent), store_for=lambda _: store,
        runtime_paths=lambda _: {'subagent_workspace': workspace})
    seen = set()
    services = SimpleNamespace(resources=resources, allows=lambda auth, kind, value: not seen.add(value))
    monkeypatch.setattr(gateway_pending, 'resolve_read_context', lambda _: context)
    monkeypatch.setattr(gateway_pending, 'pending_inputs', lambda *args: [])
    monkeypatch.setattr(store.tasks, 'list_report', lambda *a, **k: pytest.fail('不得全史回退'))
    for _ in range(8):
        try:
            rows = gateway_pending.pending_interactions(services, object(), thread.thread_id)
        except HostAPIError as error:
            assert error.status == 503
        else:
            assert rows == [] and len(seen) == 301  # 当前root之外仍有原active根。
            break
    else:
        pytest.fail('300根必须跨poll最终覆盖，不能永久503')


def test_root_round_progress_survives_small_lru_and_long_history_per_root(tmp_path, monkeypatch):
    agent, first, _ = _approval_tree(tmp_path)
    store = agent.conversation_store
    thread_id = store.tasks.load(first.root_id).thread_id
    roots = [first.root_id]
    for number in range(6):
        root = f'fair-root-{number}'
        store.tasks.bind({'thread_id': thread_id, 'task_id': root, 'status': 'completed', 'goal': 'parent'})
        child = agent.subagents.create_run(goal='pending work', root_id=root, parent_id=root)
        child = agent.subagents.lifecycle.prepare_runner_attempt(child.id)
        approvals.publish_agent_tool_approval(agent, run_id=child.id, thread_id=child.agent_thread_id,
            request_value=_child_approval_request(child.id))
        roots.append(root)
    for root in roots:
        directory = store.storage.root / 'subagent_tool_approvals' / root
        for number in range(200):
            (directory / f'old-{number:04d}.json').write_text('{"status":"approved"}')
    approvals._pending_discovery_scans.close()
    approvals._pending_discovery_windows.clear()
    monkeypatch.setattr(approvals, '_PENDING_DISCOVERY_WINDOW_ROOTS_COUNT', 2)
    observed = []
    report = approvals.list_pending_agent_tool_approvals_report
    def counted(*args, **kwargs):
        result = report(*args, **kwargs)
        observed.append(kwargs['scan_progress'].get('entries', 0))
        return result
    monkeypatch.setattr(approvals, 'list_pending_agent_tool_approvals_report', counted)
    for _ in range(24):
        start = len(observed)
        try:
            rows = gateway_pending._pending_approval_round(agent, store, thread_id, tuple(sorted(roots)), deadline=time.monotonic() + 10)
        except HostAPIError as error:
            assert error.status == 503
        else:
            assert len(rows) == 7
            break
        assert sum(observed[start:]) <= 128
    else:
        pytest.fail('根游标必须越过LRU容量，不能每拍重扫第一根')
    # 完整轮次复用当前refs；即使根级LRU只剩2项也不再扫描七根历史。
    observed.clear()
    rows = gateway_pending._pending_approval_round(agent, store, thread_id, tuple(sorted(roots)), deadline=time.monotonic() + 10)
    assert len(rows) == 7 and observed == []


@pytest.mark.parametrize('portable', [False, True])
def test_old_schema_heartbeat_installs_generation_before_body_without_changing_inventory(tmp_path, monkeypatch, portable):
    import subprocess
    import sys

    workspace, index, _, _, tasks = _current_set(tmp_path, 1)
    with index.connect() as db:
        expected = db.execute("SELECT key,value FROM meta WHERE key IN ('schema','complete','offset','inventory_ready') ORDER BY key").fetchall()
        db.execute("DELETE FROM meta WHERE key='current_membership_revision'")
    with pytest.raises(SubagentRunIndexUnavailable):
        SubagentRunIndex(workspace, read_only=True).current_membership_revision()
    if portable:
        from agent_py_agent.agent.subagents.services.persistence import run_index
        monkeypatch.setattr(run_index.nofollow_fs, '_supports_dir_fd', lambda: False)
    # 沿已经存在的writer对象同状态心跳；没有重新构造install，也没有成员变更。
    index.upsert(replace(tasks[0], goal='heartbeat', updated_at=time.time()))
    cold = SubagentRunIndex(workspace, read_only=True)
    assert cold.current_membership_revision() > 0
    with cold.connect() as db:
        assert db.execute("SELECT key,value FROM meta WHERE key IN ('schema','complete','offset','inventory_ready') ORDER BY key").fetchall() == expected
    code = ('import sys; from pathlib import Path; '
            'from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex; '
            'print(SubagentRunIndex(Path(sys.argv[1]),read_only=True).current_membership_revision())')
    assert int(subprocess.check_output([sys.executable, '-c', code, str(workspace)], text=True)) == cold.current_membership_revision()
    with index.connect() as db:
        db.execute("DELETE FROM meta WHERE key='current_membership_revision'")
    with pytest.raises(RuntimeError, match='body rollback'):
        with index.connect() as db:
            db.execute('BEGIN')
            raise RuntimeError('body rollback')
    assert SubagentRunIndex(workspace, read_only=True).current_membership_revision() > 0


def test_abandoned_current_discovery_slots_expire_without_losing_live_progress(tmp_path, monkeypatch):
    workspace, _, store, thread, _ = _current_set(tmp_path, 129)
    clock = [0.0]
    monkeypatch.setattr(gateway_pending, '_pending_cache_clock', lambda: clock[0])
    monkeypatch.setattr(gateway_pending, '_PENDING_DISCOVERY_CACHE_LIMIT_COUNT', 2)
    gateway_pending._current_approval_roots.clear()
    for name in ('abandoned-one', 'abandoned-two'):
        _, complete = gateway_pending._current_child_approval_roots(workspace, store, name,
            deadline=time.monotonic() + 10, scan_limit=1)
        assert not complete
    first_key = (str(workspace), 'abandoned-one')
    first = gateway_pending._current_approval_roots[first_key]
    first['in_use'] = True
    clock[0] = 61.0
    _, complete = gateway_pending._current_child_approval_roots(workspace, store, thread.thread_id,
        deadline=time.monotonic() + 10, scan_limit=1)
    assert not complete
    assert gateway_pending._current_approval_roots[first_key] is first
    assert first['after'] == 'run-0000' and first['in_use']
    assert (str(workspace), 'abandoned-two') not in gateway_pending._current_approval_roots
    roots, complete = gateway_pending._current_child_approval_roots(workspace, store, thread.thread_id,
        deadline=time.monotonic() + 10, scan_limit=128)
    assert complete and roots == {'root'}
    first['in_use'] = False


def test_abandoned_root_round_slots_expire_but_locked_round_is_preserved(tmp_path, monkeypatch):
    _, _, store, _, _ = _current_set(tmp_path, 1)
    clock = [0.0]
    monkeypatch.setattr(gateway_pending, '_pending_cache_clock', lambda: clock[0])
    monkeypatch.setattr(gateway_pending, '_PENDING_DISCOVERY_CACHE_LIMIT_COUNT', 2)
    gateway_pending._approval_root_rounds.clear()
    first = gateway_pending._approval_round(store, 'one', ('root-one',))
    gateway_pending._approval_round(store, 'two', ('root-two',))
    clock[0] = 30.0
    with pytest.raises(HostAPIError):
        gateway_pending._approval_round(store, 'three', ('root-three',))
    clock[0] = 61.0
    first['lock'].acquire()
    try:
        third = gateway_pending._approval_round(store, 'three', ('root-three',))
        assert gateway_pending._approval_root_rounds[(str(store.storage.root), 'one')] is first
        assert tuple(first['queue']) == ('root-one',)
        assert (str(store.storage.root), 'two') not in gateway_pending._approval_root_rounds
        assert tuple(third['queue']) == ('root-three',)
    finally:
        first['lock'].release()
    clock[0] = 122.0
    fourth = gateway_pending._approval_round(store, 'four', ('root-four',))
    assert tuple(fourth['queue']) == ('root-four',)
    assert len(gateway_pending._approval_root_rounds) == 1
