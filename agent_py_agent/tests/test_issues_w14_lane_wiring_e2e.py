"""W14：从真实单进程维护入口核验车道接线、责任保护与 owner 隔离。"""
from __future__ import annotations

import gc
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.assets.mutation_ledger import freeze_asset_mutations
from agent_py_agent.agent.capability.asset_evolution import AssetEvolutionService
from agent_py_agent.agent.capability.asset_evolution_routes import write_evolution_json
from agent_py_agent.agent.common.json_io import write_private_text_file_atomic
from agent_py_agent.agent.conversation import ConversationStore, message_scan
from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.display_archive import (
    archive_display_rows,
    read_display_archive_page,
)
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.conversation.session_task_report import result_report_key
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.agent.gateway_parts import gateway_history_index
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage import maintenance as local_maintenance
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.memory_store.retired_thread_journal import append_retired_thread
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.services.persistence.projections import (
    _write_owner_agent_projection,
)
from agent_py_agent.agent.tooling.process_session_records import PROCESS_SESSION_SCHEMA
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.user_space import storage_lanes
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)
from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
from agent_py_agent.agent.user_space.storage_lanes import LANE_BINDINGS, validate_lane_receipt
from agent_py_agent.cli import gateway_loops


def _controller(home, monkeypatch):
    """仅提供 Gateway 上下文和启动时钟；分页、due、dispatcher 与领域写者全部真实。"""
    agent = SimpleNamespace(home_paths=home, config=SimpleNamespace(
        owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64))
    monkeypatch.setattr(gateway_loops, '_gateway_agent_from_context', lambda context: agent)
    clock = SimpleNamespace(now=1000.0)
    proxy = SimpleNamespace(**vars(gateway_loops.time))
    proxy.monotonic = lambda: clock.now
    monkeypatch.setattr(gateway_loops, 'time', proxy)
    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace())
    clock.now += gateway_loops.STARTUP_GRACE_WINDOW_SECONDS + 1
    return controller


def _terminal_task(store, now, *, key='settled', metadata=None):
    """用任务状态机及唤醒写者生成终态和已处理回报，不伪造状态或发布证明。"""
    sender = store.threads.create(owner_id='local/main', title='发送方').thread_id
    target = store.threads.create(owner_id='local/main', title='接收方').thread_id
    task = store.session_tasks.create(SessionTaskDraft(sender, target, '原始正文' * 8000,
        dedupe_key=key, body_dedupe_key=key), now=now - 32 * 86400)
    store.session_tasks.advance(task.task_id, SessionTaskUpdate('accepted'), now=now - 32 * 86400)
    task = store.session_tasks.advance(task.task_id, SessionTaskUpdate('done', summary='原始摘要' * 8000),
        now=now - 31 * 86400)
    if metadata is not None:
        update_json_file_atomic(store.session_tasks._path(task.task_id),
            lambda row: {**row, 'metadata': metadata}, require_existing=True)
    wake = store.wakes.raise_signal({'thread_id': sender, 'reason': 'session_message',
        'dedupe_key': result_report_key(task), 'summary': '任务结果', 'retain_handled': True})
    store.wakes.mark_handled(wake.wake_signal_id)
    return task


def test_unconfirmed_cancellation_payload_survives_real_controller(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path / 'home')
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    store = ConversationStore(home.owner_home_dir / 'conversations')
    now = time.time()
    # prepared=False 单独不是现合同的未结责任；明确尚未确认的取消交接必须保留完整原记录。
    task = _terminal_task(store, now, metadata={'cancellation_control': {'resources_confirmed': False}})
    path = store.session_tasks._path(task.task_id)
    before = path.read_bytes()
    controller = _controller(home, monkeypatch)
    controller.tick(now=now)
    assert path.read_bytes() == before
    assert store.session_tasks.require(task.task_id).goal == task.goal


def _age(path, now, days):
    os.utime(path, (now - days * 86400,) * 2)
    return path


def _compact_thread(home, store, now, *, rotate=False):
    """复制既有 checkpoint 夹具的公开提交链，真实生成 CoW 树及可选转录轮转。"""
    thread = store.threads.create(owner_id=home.owner_id, title='检查点会话')
    agent = SimpleNamespace(home_paths=home, conversation_store=store,
        backend=SimpleNamespace(name='test', model_name='test-model'))
    policy = RuntimeCompactPolicy(context_window_tokens=1000, trigger_percent=90, trigger_tokens=900,
        recovery_target_percent=60, recovery_target_tokens=600, allow_persistent_apply=True,
        recent_tail_max_turns=4, recent_tail_tokens=100, failure_threshold=3, failure_cooldown_seconds=300.0)
    rows = [store.messages.append({'thread_id': thread.thread_id, 'role': 'user',
        'content': f'第 {number} 行：' + '正文' * 300, 'now': now}) for number in range(12 if rotate else 2)]
    boundary = store.messages.byte_offset_after(thread.thread_id, rows[-2].message_id)
    checkpoint_id = checkpoints.write_compact_checkpoint(agent, checkpoints.CompactCheckpointRequest(
        thread=thread, summary='原始摘要', operation_evidence={}, compact_rows=tuple(rows[:-1]),
        retained_tail=(rows[-1],), source_end_byte_offset=boundary, projected_tokens_before=900,
        projected_tokens_after=300, policy=policy, forced=False, scope=THREAD_COMPACT_SCOPE))
    updated = store.threads.update_compact_state(thread.thread_id, commit=ConversationCompactCommit(
        summary='原始摘要', operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=rows[-2].message_id, compacted_through_byte_offset=boundary,
        source_messages=len(rows) - 1, source_tool_pairs=0, publish_thread_view=True),
        expected_generation=0, now=now)
    return updated, rows


def _projection(home, repo, now, *, run_id, terminal):
    """由真实运行库与 owner 投影写者生成定位信息，不启动子代理或模型。"""
    run = repo.record_run_creation(owner_id=home.owner_id, goal='投影样本', run_id=run_id)
    if terminal:
        repo.settle_agent_run(agent_run_id=run['agent_run_id'], status='done', attempt_id=run['attempt_id'])
    workspace = home.owner_workspace_dir / run_id
    workspace.mkdir(parents=True)
    stamp = now - (8 if terminal else 40) * 86400
    task = SimpleNamespace(id=run_id, owner=home.owner_id, task_workspace_dir=str(workspace),
        agent_run_workspace_dir=str(workspace), agent_run_final_report_md='', output_json='',
        runner_result_json='', runner_result_file='', updated_at=stamp)
    _write_owner_agent_projection(SimpleNamespace(owner_home_dir=home.owner_home_dir), task,
        {'run_id': run_id, 'parent_id': 'parent', 'root_id': 'root', 'task_id': 'task',
         'status': 'DONE' if terminal else 'RUNNING', 'created_at': now - 40 * 86400,
         'updated_at': stamp, 'result_summary': '原始结果', 'error_summary': '',
         'usage_summary': {'tokens': 1}, 'deliverable_paths': [str(workspace / 'output.md')]})
    root = home.owner_home_dir / 'agents' / run_id
    write_private_text_file_atomic(root / 'workspace_locator.json', json.dumps({'workspace_root': str(workspace)}))
    write_private_text_file_atomic(root / 'events.jsonl', '{"event":"原事件"}\n' * 1000)
    for path in root.iterdir():
        _age(path, now, 8 if terminal else 40)
    _age(root, now, 8 if terminal else 40)
    return root


def _process_log(home, now, *, session_id, terminal):
    """只登记真实进程存储记录；到期裁剪和日志退役留给维护入口执行。"""
    jobs = home.owner_home_dir / '.background_jobs'
    jobs.mkdir(exist_ok=True)
    output = jobs / ('job-1.log' if terminal else 'job-2.log')
    write_private_text_file_atomic(output, '后台原始输出\n' * 1000)
    owner = str(home.owner_home_dir)
    store = ProcessSessionStore(process_session_store_root(owner, owner))
    store.write({'schema': PROCESS_SESSION_SCHEMA, 'session_id': session_id, 'revision': 0,
        'access_scope': {'owner_id': home.owner_id, 'conversation_id': 'thread-a', 'owner_home': owner},
        'execution_scope': {'owner_home': owner, 'thread_id': 'thread-a', 'root_task_id': 'task-a',
                            'run_id': 'run-a', 'attempt_id': 'attempt-a'},
        'activation_scope': None, 'retain_until_consumed': False, 'completion_target': {},
        'completion_notice_id': '', 'command': 'fixture only', 'cwd': owner, 'output_file': str(output),
        'host_state_file': '', 'launcher_pid': 100, 'launcher_birth_token': 'launcher-birth',
        'pid': 101, 'pid_birth_token': 'host-birth', 'child_pid': 102, 'child_pid_birth_token': 'child-birth',
        'reserved_at': now - 9 * 86400, 'started_at': now - 9 * 86400,
        'finished_at': now - 8 * 86400 if terminal else None, 'exit_code': 0 if terminal else None,
        'status': 'exited' if terminal else 'running', 'child_launch_started': True,
        'stop_requested': False, 'handoff_confirmed': False})
    if terminal:
        # 先按真实 Store 收尾协议搬入历史；过期删除和日志退役仍由受测维护车道执行。
        assert store.prune_finished(max_age_seconds=None, now=now) == []
        assert (store.root / 'finished' / (session_id + '.json')).is_file()
    return output


def _tool_output(home, repo, now, *, name, settled):
    run = repo.record_run_creation(owner_id=home.owner_id, run_id='output-' + name, goal='原工具输出')
    stamp = now - 31 * 86400
    if settled:
        repo.settle_agent_attempt(agent_run_id=run['agent_run_id'], attempt_id=run['attempt_id'], now=stamp)
    record = externalize_tool_output_record(ExternalizeToolOutputRequest(root=home.owner_home_dir,
        tool='sample', call_id=name, output='原始工具结果' * 1000, ok=True, run_id='output-' + name,
        attempt_id=run['attempt_id'], min_chars=1, preview_chars=10))
    path = Path(record['output_path'])
    value = json.loads(path.read_text())
    value['created_at'] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    write_private_text_file_atomic(path, json.dumps(value, ensure_ascii=False))
    index = path.parent / 'index.jsonl'
    rows = [json.loads(line) for line in index.read_text().splitlines()]
    for row in rows:
        if row.get('path') == str(path):
            row['created_at'] = value['created_at']
    write_private_text_file_atomic(index, ''.join(json.dumps(row) + '\n' for row in rows))
    return _age(path, now, 31)


def _display(store, home, thread_id, now, *, days):
    ref = archive_display_rows(SimpleNamespace(conversation_store=store), thread_id=thread_id,
        rows=[{'text': '展示原文' * 8000}])
    root = store.storage.root / 'display_archives' / ref['archive_id']
    _age(root / 'manifest.json', now, days)
    return root


def _evolution(home, now, *, name, processed):
    value = freeze_asset_mutations(home.owner_home_dir,
        {'asset_mutations': {'evidence_tags': ['query_failed']}}, run_id=name)
    if processed:
        assert AssetEvolutionService(home.owner_home_dir).process(name)['status'] == 'query_failed'
        value['result'] = {'status': 'query_failed', 'notices': [], 'routes': []}
    value['created_at'] = datetime.fromtimestamp(now - 15 * 86400, timezone.utc).isoformat()
    parts = ('data', 'asset_evolution', 'requests', name + '.json')
    write_evolution_json(home.owner_home_dir, parts, value)
    return home.owner_home_dir.joinpath(*parts)


def _live_owner(home, now, *, managed_process=True):
    """同时提供每类可观测的活跃保护对象，文件内容和数据库事实分别核对。"""
    store = ConversationStore(home.owner_home_dir / 'conversations')
    thread = store.threads.create(owner_id=home.owner_id, title='仍在使用的会话')
    store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '保留当前消息'})
    wake = store.wakes.raise_signal({'thread_id': thread.thread_id, 'reason': 'agent_event', 'summary': '待处理'})
    repo = RuntimeRepository(home.owner_home_dir / 'runtime.db')
    projection = _projection(home, repo, now, run_id='live-child', terminal=False)
    log = _process_log(home, now, session_id='bg-live', terminal=False) if managed_process else None
    output = _tool_output(home, repo, now, name='live-call', settled=False)
    archive = _display(store, home, thread.thread_id, now, days=0)
    pending = _evolution(home, now, name='pending-evolution', processed=False)
    paths = [store.storage.thread_path(thread.thread_id), store.storage.message_path(thread.thread_id),
             projection / 'state.json', projection / 'events.jsonl', output, pending]
    if log is not None:
        paths.append(log)
    # 展示副本允许一天后无损合页；manifest 和公开读页的完整结果才是应保留的事实。
    paths.append(archive / 'manifest.json')
    archive_ref = json.loads((archive / 'manifest.json').read_text())
    archive_pages = [read_display_archive_page(store, archive_ref, number)
                     for number in range(archive_ref['page_count'])]
    protected = {path: path.read_bytes() for path in paths}
    return SimpleNamespace(home=home, store=store, thread=thread, wake=wake, protected=protected,
        archive_ref=archive_ref, archive_pages=archive_pages, repo=repo)


@pytest.fixture
def lane_world(tmp_path, monkeypatch):
    """只缩小转录切片及事件保留数量，真实生成通道到期样本并保留原策略/算法。"""
    monkeypatch.setattr(message_scan, 'TRANSCRIPT_ROTATE_BYTES', 1024)
    monkeypatch.setattr(message_scan, '_TRANSCRIPT_SEGMENT_BYTES', 1024)
    monkeypatch.setattr(local_maintenance, 'STEADY_STATE_EVENTS_KEEP_LAST', 3)
    home = ensure_my_agent_home(tmp_path / 'home')
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    now = time.time()
    live = _live_owner(home, now)
    store, repo = live.store, live.repo

    retired, _ = _compact_thread(home, store, now)
    compact_root = home.owner_compact_dir
    # 夹具模拟已经完成的线程退休；删除依据仍由正式 journal writer 提供，绝不以孤儿猜测授权。
    store.storage.thread_path(retired.thread_id).unlink()
    append_retired_thread(home, thread_id=retired.thread_id, conversation_root=store.storage.root,
        action_id='retired-thread-fixture', trash_container=home.owner_data_dir / 'trash' / 'purged', retired_at=now - 1)
    retired_tree = compact_root / 'evidence' / retired.thread_id
    assert retired_tree.is_dir()

    active, messages = _compact_thread(home, store, now, rotate=True)
    message_path = store.storage.message_path(active.thread_id)
    manifest = message_scan.transcript_manifest(message_path)
    assert manifest is not None and manifest['segment_count'] > 1
    index = ImmutableJsonIndex(message_path.parent, ('archive', active.thread_id, 'index'))
    orphan = index._save({'entries': {'superseded': True}})
    orphan_path = _age(message_path.parent.joinpath(*index.parts, orphan['ref']['ref']), now, 8)
    archive = _display(store, home, live.thread.thread_id, now, days=15)
    wake = store.wakes.raise_signal({'thread_id': live.thread.thread_id, 'reason': 'agent_event',
        'dedupe_key': 'old-handled', 'summary': '已处理的旧唤醒', 'now': now - 15 * 86400})
    store.wakes.mark_handled(wake.wake_signal_id)
    wake_path = store.storage.wake_handled_dir / (wake.wake_signal_id + '.json')
    update_json_file_atomic(wake_path, lambda row: {**row, 'handled_at': now - 15 * 86400}, require_existing=True)
    log = _process_log(home, now, session_id='bg-due', terminal=True)

    gateway_root = runtime_paths_for_agent(None, home.owner_home_dir, home)['gateway_workspace']
    history = gateway_history_index.GatewayHistoryIndex(gateway_paths_from_root(gateway_root))
    # 兼容热段已满后，由真实 writer 生成过期日段，避免空历史库跳过写门。
    with history.connection() as conn:
        history._set_meta(conn, 'compat_count', 128)
    history_time = now - (MemoryRetentionPolicy().audit_days + 1) * 86400
    history_payload = {'id': 'expired-gateway-request', 'status': 'done', 'ended_at': history_time}
    with monkeypatch.context() as patch:
        patch.setattr(gateway_history_index, 'time', SimpleNamespace(time=lambda: history_time))
        assert history.append(history_payload)
    history_day = datetime.fromtimestamp(history_time, timezone.utc).strftime('%Y-%m-%d')
    history_segment = gateway_root / 'history_segments' / (history_day + '.jsonl')
    assert history.get(history_payload['id']) == history_payload and history_segment.is_file()

    local = LocalStore(home.owner_workspace_dir / 'runtime/workspaces/fixture/local_store/local.db')
    with local.connection_batch():
        events = [local.record_event('fixture', payload={'number': number, 'padding': '原事件' * 2000}).event_id
                  for number in range(6)]
    projection = _projection(home, repo, now, run_id='done-child', terminal=True)
    task = _terminal_task(store, now)
    pending_task = _terminal_task(store, now, key='cancel-pending',
        metadata={'cancellation_control': {'resources_confirmed': False}})
    pending_path = store.session_tasks._path(pending_task.task_id)
    live.protected[pending_path] = pending_path.read_bytes()
    output = _tool_output(home, repo, now, name='done-call', settled=True)
    evolution = _evolution(home, now, name='done-evolution', processed=True)

    other_identity = OwnerIdentity.provider_user('feishu', 'second-owner')
    other = home_paths_with_owner(home, ensure_owner_home(home.root, other_identity))
    other.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    # 第二 owner 只有活业务数据；进程域的活跃保护由第一个 owner 的真实进程记录覆盖。
    second = _live_owner(other, now, managed_process=False)
    return SimpleNamespace(home=home, now=now, live=live, second=second, retired=retired,
        retired_tree=retired_tree, active=active, messages=messages, orphan=orphan_path,
        archive=archive, wake=wake, wake_path=wake_path, log=log, local=local, events=events,
        event_jsonl=local.events_path.read_bytes(), history=history, history_payload=history_payload,
        history_segment=history_segment, projection=projection, task=task, output=output, evolution=evolution)


def _assert_live(owner):
    for path, raw in owner.protected.items():
        assert path.is_file(), path
        assert path.read_bytes() == raw, path
    assert owner.store.wakes.pending_one(owner.wake.wake_signal_id) is not None
    assert owner.store.threads.require(owner.thread.thread_id).title == owner.thread.title
    assert [read_display_archive_page(owner.store, owner.archive_ref, number)
            for number in range(owner.archive_ref['page_count'])] == owner.archive_pages


def _assert_retired(world):
    """检查真实领域后像，不将完成回执或检查计数冒充删除结果。"""
    # generation及恢复意图的锁锚不能unlink换inode；仅精确空锚可留，业务载荷必须全消失。
    remaining = {path.relative_to(world.retired_tree) for path in world.retired_tree.rglob('*') if path.is_file()}
    assert remaining <= {Path(namespace) / ('.nodes.sqlite3' + suffix)
                         for namespace in ('indexes', 'timeline')
                         for suffix in ('.generation.lock', '.recovery-intent.json.lock')}
    assert all((world.retired_tree / path).stat().st_size == 0 for path in remaining)
    assert not any(path.is_symlink() for path in world.retired_tree.rglob('*'))
    assert not (world.home.owner_compact_dir / 'conversations' / (world.retired.thread_id + '.jsonl')).exists()
    assert not world.orphan.exists()
    manifest = json.loads((world.archive / 'manifest.json').read_text())
    assert manifest['expired'] is True
    assert list(world.archive.iterdir()) == [world.archive / 'manifest.json']
    assert not world.wake_path.exists()
    assert world.live.store.wakes.delivery_receipt(world.wake.thread_id, world.wake.dedupe_key) == ''
    assert not world.log.exists()
    with sqlite3.connect(world.local.db_path) as conn:
        ids = [row[0] for row in conn.execute('SELECT event_id FROM events ORDER BY seq')]
    assert ids == world.events[-3:]
    # SQLite 裁剪不重写活跃追加文件，封存前仍保留完整原事件。
    assert world.local.events_path.read_bytes() == world.event_jsonl
    assert [json.loads(line)['event_id'] for line in world.event_jsonl.splitlines()] == world.events
    assert world.history.get(world.history_payload['id']) is None
    assert not world.history_segment.exists()
    assert not (world.projection / 'events.jsonl').exists()
    assert (world.projection / 'subagent_compact.json').is_file()
    task = world.live.store.session_tasks.require(world.task.task_id)
    assert task.status == 'done' and task.goal == task.summary == '' and 'payload_retired' in task.metadata
    assert not world.output.exists() and not world.evolution.exists()
    for message in world.messages:
        row, errors = world.live.store.messages.by_id_report(world.active.thread_id, message.message_id)
        assert not errors and row == message
    _assert_live(world.live)
    _assert_live(world.second)


def _observe_state(home, *, coordinator):
    marker = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
    state = marker['storage_maintenance']
    assert state['errors'] == []
    if coordinator:
        assert state['coordinator_lanes']['fallback_kinds'] == []
        assert state['coordinator_lanes']['errors'] == []
        return marker, set(state['coordinator_lanes']['ran'])
    assert state['unbound_kinds'] == []
    assert state['lane_fallback_kinds'] == []
    for kind, receipt in state['lane_reports'].items():
        validate_lane_receipt({**receipt, 'cursor': state['cursors'][kind]})
        assert receipt.get('errors', []) == [], (kind, receipt)
    return marker, set(state['lane_reports'])


def _fd_count(root: Path | str | None = None):
    """读取真实 FD 表；指定目录时仅统计其原路径或规范路径下的描述符。"""
    prefixes = () if root is None else tuple({str(Path(root)), str(Path(root).resolve())})
    for path in (Path('/proc/self/fd'), Path('/dev/fd')):
        if path.is_dir():
            with os.scandir(path) as entries:
                if root is None:
                    return sum(1 for _ in entries)
                count = 0
                for entry in entries:
                    if path == Path('/proc/self/fd'):
                        try:
                            target = os.readlink(entry.path)
                        except OSError:
                            continue
                    else:
                        import fcntl

                        try:
                            target = fcntl.fcntl(int(entry.name), getattr(fcntl, 'F_GETPATH', 50),
                                bytes(1024)).split(b'\x00', 1)[0].decode('utf-8', 'surrogateescape')
                        except (OSError, ValueError):
                            continue
                    if any(target == prefix or target.startswith(prefix + os.sep) for prefix in prefixes):
                        count += 1
                return count
    if os.name == 'nt':
        import ctypes

        count = 0
        for descriptor in range(ctypes.CDLL('ucrtbase')._getmaxstdio()):
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            count += 1
        return count
    raise AssertionError('当前平台没有真实 FD 计数入口')


# LLM: 两个 W14 真实入口共享有界收敛测量；负差值不归因于维护，持续正增长仍然失败。
# 函数用途: 等待最多两秒回收临时 FD，再返回真实描述符差值。
def _settled_fd_delta(before_fds, root: Path | str | None = None):
    """在同一观测范围内等待有界收敛，持续的正增长仍返回给调用方判定失败。"""
    deadline = time.monotonic() + 2.0
    while True:
        gc.collect()
        delta = _fd_count(root=root) - before_fds
        if delta <= 0 or time.monotonic() >= deadline:
            return delta
        time.sleep(0.05)


@pytest.mark.parametrize('table', ['/proc/self/fd', '/dev/fd'])
def test_fd_count_scopes_targets_without_changing_process_wide(tmp_path, monkeypatch, table):
    """隔离两种平台的目标读取合同，验证路径边界、别名、异常和枚举器关闭。"""
    import sys

    root, canonical = tmp_path / 'owner', tmp_path / 'canonical-owner'
    table = Path(table)
    targets = [str(root), str(canonical / 'held.txt'), str(root) + '-other/held.txt',
               str(tmp_path / 'outside.txt'), None]
    entries = [SimpleNamespace(name=str(number), path=str(table / str(number)))
               for number in range(len(targets))]
    closed, lookups = [], []
    original_resolve = Path.resolve

    class Directory:
        def __enter__(self):
            return iter(entries)

        def __exit__(self, *args):
            closed.append(True)

    def resolve(path, *args, **kwargs):
        return canonical if path == root else original_resolve(path, *args, **kwargs)

    def target(number):
        lookups.append(number)
        value = targets[number]
        if value is None:
            raise OSError('descriptor closed during observation')
        return value

    def fcntl(descriptor, command, buffer):
        assert command == 50 and buffer == bytes(1024)
        if descriptor == len(targets) - 1:
            raise ValueError('descriptor unavailable')
        return target(descriptor).encode('utf-8') + b'\x00unused'

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'is_dir', lambda path: path == table)
        patch.setattr(Path, 'resolve', resolve)
        patch.setattr(os, 'scandir', lambda path: Directory())
        patch.setattr(os, 'readlink', lambda path: target(int(Path(path).name)))
        patch.setitem(sys.modules, 'fcntl', SimpleNamespace(fcntl=fcntl))
        assert _fd_count() == len(entries)
        assert lookups == [], '默认进程级计数不能依赖描述符目标解析'
        assert _fd_count(root=root) == 2
        assert _fd_count(root=str(root)) == 2
    assert closed == [True, True, True]


# LLM: 保持真实文件 FD 超过收敛窗口，确保测量不会把持续增长吞成零。
# 函数用途: 用真实持有和释放验证泄漏检测仍然有效。
def test_settled_fd_delta_detects_persistent_growth(tmp_path):
    gc.collect()
    before_fds = _fd_count(root=tmp_path)
    descriptor = os.open(tmp_path / 'held.txt', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        assert _settled_fd_delta(before_fds, root=tmp_path) > 0
    finally:
        os.close(descriptor)
    assert _settled_fd_delta(before_fds, root=tmp_path) <= 0


def _lane_service_clock(monkeypatch):
    """预算钟只按真实车道推进，避免宿主磁盘抖动改变完整轮次；墙钟限额另计。"""
    wall_clock = time.monotonic
    spent = [wall_clock()]
    original = storage_lanes.resolve_storage_lane

    def resolve(kind):
        lane = original(kind)
        if lane is None:
            return None

        def execute(request):
            try:
                return lane(request)
            finally:
                # 不替换车道结果、计数或写门，只让一轮确定性分片，不能依赖机器恰好够快。
                spent[0] += 0.040
        return execute

    monkeypatch.setattr(time, 'monotonic', lambda: spent[0])
    monkeypatch.setattr(storage_lanes, 'resolve_storage_lane', resolve)
    return wall_clock


def test_controller_wires_all_lanes_and_preserves_second_owner(lane_world, monkeypatch):
    world = lane_world
    controller = _controller(world.home, monkeypatch)
    before = owner_logical_usage_bytes(world.home.owner_home_dir, exact=True)
    seen, revisited = set(), set()
    first_round_complete = repeated_all = False
    _lane_service_clock(monkeypatch)
    gc.collect()
    before_fds = _fd_count(root=world.home.root)
    # R2 的 complete 表示扫描一轮完成，不表示 pending 宽限证明已消费；继续驱动后续周期。
    now = world.now
    for tick in range(24):
        summary = controller.tick(now=now)
        assert summary['failed'] == summary['refused'] == summary['isolated'] == 0
        marker, kinds = _observe_state(world.home, coordinator=False)
        if first_round_complete:
            revisited.update(kinds)
        seen.update(kinds)
        if tick == 0:
            assert world.orphan.exists(), '七天宽限期前不能清扫旧索引节点'
        if not marker['maintenance_partial']:
            if first_round_complete:
                repeated_all = revisited == set(LANE_BINDINGS)
            elif seen == set(LANE_BINDINGS):
                first_round_complete = True
        if not marker['maintenance_partial'] and not world.orphan.exists() and repeated_all:
            break
        # 完整一轮后遵守原一天维护间隔；未完成片只推进一秒，不缩短七天宽限期。
        now += 7 * 86400 if tick == 0 else (1 if marker['maintenance_partial'] else
            MemoryRetentionPolicy().maintenance_interval_seconds)
    else:
        pytest.fail('真实控制器未在有界拍数内消费宽限证明并重新走完全部登记车道')
    assert _settled_fd_delta(before_fds, root=world.home.root) <= 0
    assert seen == revisited == set(LANE_BINDINGS)
    assert repeated_all, '首轮真正完成后，必须再逐片走完全部车道并完成第二轮'
    _assert_retired(world)
    assert owner_logical_usage_bytes(world.home.owner_home_dir, exact=True) < before
