"""Wave 3 consolidated tests (test_consolidated_c2_g3).

- test_r8_background_transcript_bytes.py: 展示缓存跨线程字节门与原游标恢复；不触碰canonical或执行权限。
- test_r8_compact_progress_pulse.py: 摘要等待可观察、覆盖数不虚增，退出后不再发表旧进度。
- test_r8_context_memo_bounds.py: 临时memo键有界，不裁剪真实历史与估算输入。
- test_r8_dispatch_log_retention.py: 真实stdout SH锁、退出证明与canonical业务终态共同约束日志保留。
- test_r8_exact_task_readers.py: 已知TaskLink身份的读者不能再遍历线程历史；坏目标仍保持原未知边界。
- test_r8_fts_cold_discovery.py: 冷发现只借首片预算；真实旧job、原生续点和普通维护写门保持。
- test_r8_idle_claim_history.py: 历史任务窗口化以后，孤立旧 claim 仍必须阻止闲置压缩。
"""
import json
import os
import shutil
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import ContextVar
from threading import (
    Event,
    Lock,
)
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import run_task_workspace_writer as workspace
from agent_py_agent.agent.agent_core.model import context_pressure as pressure
from agent_py_agent.agent.agent_core.model.context_estimate_cache import ContextEstimateMemo
from agent_py_agent.agent.agent_core.orchestration.background.log_retention import (
    pin_dispatch_log,
    record_dispatch_log_exit,
    retire_dispatch_logs_slice,
)
from agent_py_agent.agent.agent_core.tool_loop import completion
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.agent.common.json_io import write_private_json_file_atomic_unlocked as write
from agent_py_agent.agent.conversation import (
    ConversationStore,
    runtime,
    store_tasks,
)
from agent_py_agent.agent.conversation import (
    background_transcript as transcript,
)
from agent_py_agent.agent.conversation import (
    claim_activity_projection as view,
)
from agent_py_agent.agent.conversation import (
    compact_progress_pulse as module,
)
from agent_py_agent.agent.conversation.run_claim import detached_task_claim_scope_id
from agent_py_agent.agent.ingestion.source_worker import audit_parent_reconcile_state
from agent_py_agent.agent.local_storage import (
    LocalStore,
    maintenance,
)
from agent_py_agent.agent.local_storage import (
    fts_upgrade as upgrade,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.user_space.directory_paging import directory_page
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
from agent_py_agent.tests.test_model_latency_q2 import _context_case

# === Source: agent_py_agent/tests/test_r8_background_transcript_bytes.py ===

def append(agent, thread, index, payload):
    return transcript.append_background_transcript_event(
        agent, source=transcript.TranscriptSource(thread, 'task', 'request'),
        kind='assistant_completed', phase='completed', block_id=f'block-{index}', payload=payload)


def test_ten_threads_share_one_byte_budget_and_latest_frame_remains(monkeypatch):
    monkeypatch.setattr(transcript, 'BACKGROUND_TRANSCRIPT_MAX_BYTES', 128 * 1024)
    agent = SimpleNamespace()
    for index in range(200):
        append(agent, f'thread-{index % 10}', index, {'text': str(index) + 'x' * 12000})
    rows = agent._conversation_background_transcript_projection
    assert sum(state.retained_bytes for state in rows.values()) <= 128 * 1024
    assert sum(len(state.events) for state in rows.values()) < 200
    last = transcript.read_background_transcript_events(agent, thread_id='thread-9', after=0)
    assert last['events'][-1]['block_id'] == 'block-199'
    assert last['truncated'] is True
    cursor = last['cursor']
    again = transcript.read_background_transcript_events(agent, thread_id='thread-9', after=cursor, stream_id=last['stream_id'])
    assert again['events'] == [] and again['truncated'] is False
    last['events'][-1]['payload']['text'] = 'client mutation'
    assert rows['thread-9'].events[-1]['payload']['text'] != 'client mutation'


def test_large_nested_payload_does_not_call_deepcopy_or_lose_terminal_marker(monkeypatch):
    agent = SimpleNamespace()
    original = transcript.copy.deepcopy
    copied = []

    def tracked(value):
        copied.append(value)
        return original(value)

    monkeypatch.setattr(transcript.copy, 'deepcopy', tracked)
    oversized = {'text': 'x' * 1000000, 'nested': [list(range(10000))]}
    sequence = append(agent, 'thread', 1, oversized)
    assert copied == []
    result = transcript.read_background_transcript_events(agent, thread_id='thread', after=0)
    assert result['cursor'] == sequence and result['truncated']
    event = result['events'][-1]
    assert event['kind'] == 'assistant_completed' and event['phase'] == 'completed'
    assert event['payload']['history_incomplete'] and event['payload']['truncated']
    assert len(event['payload']['text']) <= transcript.BACKGROUND_TRANSCRIPT_TEXT_LIMIT_CHARS
    state = agent._conversation_background_transcript_projection['thread']
    assert state.retained_bytes <= transcript.BACKGROUND_TRANSCRIPT_EVENT_MAX_BYTES


def test_cyclic_payload_and_new_task_preserve_bounded_accounting():
    agent = SimpleNamespace()
    payload = {}
    payload['cycle'] = payload
    append(agent, 'thread', 1, payload)
    state = agent._conversation_background_transcript_projection['thread']
    assert state.events[-1]['payload']['history_incomplete']
    transcript.begin_background_transcript_turn(agent, thread_id='thread', task_id='next')
    assert not state.events and not state.sizes and state.retained_bytes == 0
    other = SimpleNamespace()
    append(other, 'thread', 2, {'text': 'other owner'})
    assert not state.events


def test_oversized_completed_frame_keeps_its_readable_archive(tmp_path):
    from agent_py_agent.agent.conversation.display_archive import (
        archive_display_rows,
        read_display_archive_page,
    )
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conversation")
    agent = SimpleNamespace(conversation_store=store)
    thread = store.threads.get_or_create({"canonical_user_id": "u", "channel": "chat",
                                          "channel_conversation_id": "session", "channel_user_id": "u"})
    text = "完整原文" * 30000
    ref = archive_display_rows(agent, thread_id=thread.thread_id, rows=[{"text": text}])
    append(agent, thread.thread_id, 1, {"text": text, "display_archive_ref": ref})
    page = transcript.read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)
    payload = page["events"][0]["payload"]
    assert page["truncated"] and payload["display_archive_ref"] == ref
    restored = read_display_archive_page(store, payload["display_archive_ref"], 0)
    assert text.startswith(restored["rows"][0]["text"]) and restored["rows"][0]["text"]


def test_unknown_payload_object_cannot_run_code_under_the_display_lock():
    class Unexpected:
        def __sizeof__(self):
            raise AssertionError("must not invoke unknown object")

    agent = SimpleNamespace()
    append(agent, "thread", 1, {"unexpected": Unexpected()})
    result = transcript.read_background_transcript_events(agent, thread_id="thread", after=0)
    assert result["truncated"] and result["events"][0]["payload"]["history_incomplete"]


def test_slow_page_copy_does_not_block_another_thread_writer(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    agent = SimpleNamespace()
    append(agent, "old", 1, {"text": "old"})
    copying, resume = threading.Event(), threading.Event()
    deepcopy = transcript.copy.deepcopy

    def paused(value):
        if isinstance(value, dict) and value.get("block_id") == "block-1":
            copying.set()
            assert resume.wait(5)
        return deepcopy(value)

    monkeypatch.setattr(transcript.copy, "deepcopy", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(transcript.read_background_transcript_events, agent, thread_id="old", after=0)
        try:
            assert copying.wait(2)
            assert pool.submit(append, agent, "new", 2, {"text": "new"}).result(timeout=1) == 2
        finally:
            resume.set()
        assert read.result(timeout=2)["events"][0]["payload"] == {"text": "old"}


# === Source: agent_py_agent/tests/test_r8_compact_progress_pulse.py ===

def test_pulse_repeats_real_coverage_and_preserves_context():
    value = ContextVar('progress_owner', default='missing')
    token = value.set('owner-a')
    received = threading.Event()
    rows = []

    def report(covered, total):
        rows.append((covered, total, value.get()))
        if len(rows) >= 2:
            received.set()

    try:
        with module.compact_progress_pulse(report, interval=.01) as progress:
            progress(3, 10)
            assert received.wait(2)
        after = list(rows)
        assert after and all(row == (3, 10, 'owner-a') for row in after)
        assert progress.__self__.closed
        assert progress.__self__.call.fn is None
        assert rows == after
    finally:
        value.reset(token)


def test_pulse_zero_and_missing_callback_do_not_start_worker():
    before = module._SCHEDULER._thread
    with module.compact_progress_pulse(None, interval=5) as progress:
        assert progress is None
    with module.compact_progress_pulse(lambda *_: None, interval=0):
        assert module._SCHEDULER._thread is before


def test_pulse_failure_unsubscribes_and_never_reports_terminal():
    import pytest

    seen = threading.Event()
    with pytest.raises(RuntimeError, match='model failure'):
        with module.compact_progress_pulse(lambda *_: seen.set(), interval=.01) as progress:
            assert seen.wait(2)
            raise RuntimeError('model failure')
    assert progress.__self__.closed and progress.__self__.call.fn is None


def test_model_wait_emits_progress_without_changing_call_thread(monkeypatch):
    from agent_py_agent.agent.backends.base import ModelResponse
    from agent_py_agent.agent.conversation import compact_request_budget as budget
    from agent_py_agent.tests.test_compact_request_budget import _request

    waiting = threading.Event()
    reports = []
    caller = threading.get_ident()
    request = _request('等待模型返回的原始材料')
    request.agent.config.memory_compact_progress_heartbeat_seconds = .01

    def generate(candidate):
        assert threading.get_ident() == caller
        assert waiting.wait(2), '模型调用等待期间必须继续更新展示'
        return ModelResponse(text='摘要事实', backend='offline')

    def progress(covered, total):
        reports.append((covered, total))
        waiting.set()

    monkeypatch.setattr(budget, 'generate_auxiliary_model_response', generate)
    result = budget.generate_bounded_compact_response(request, source_progress=progress)
    assert result.text == '摘要事实'
    assert reports and set(reports) == {(0, 1)}


# === Source: agent_py_agent/tests/test_r8_context_memo_bounds.py ===

def test_large_leaf_and_wide_identity_tree_decline_cache_with_bounded_visits(monkeypatch):
    assert pressure._context_value_fingerprint('x' * 2_000_000) is None
    calls = []
    original = pressure._context_value_fingerprint

    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(pressure, '_context_value_fingerprint', observed)
    assert observed({str(i): i for i in range(100_000)}) is None
    assert len(calls) == 1


@pytest.mark.parametrize('memo_available', [False, True])
def test_uncacheable_projection_does_not_construct_output_key_and_keeps_full_history(monkeypatch, memo_available):
    from agent_py_agent.agent.agent_core.model import context_estimate_cache

    _agent, params = _context_case()
    params.tool_ir_history = [UserTurn('x' * 300_000)]
    memo = ContextEstimateMemo()
    memo.remember_prompt_projection(('old', 'old-output', 'old-prompt', ()), revision=memo.context_revision())
    monkeypatch.setattr(context_estimate_cache, 'estimate_memo_for', lambda params: memo if memo_available else None)
    calls = []
    original = pressure._prompt_history_projection_key

    def tracked(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(pressure, '_prompt_history_projection_key', tracked)
    prompt, history = pressure.project_request_prompt_history(params, 'complete prompt')
    assert prompt == 'complete prompt' and history == params.tool_ir_history
    assert len(history[0].text) == 300_000
    assert len(calls) == int(memo_available)
    if memo_available:
        assert memo.prompt_projection() is None


def test_forget_releases_old_values_and_does_not_republish_inflight_result():
    memo = ContextEstimateMemo()
    entered, release = Event(), Event()

    def compute():
        entered.set()
        assert release.wait(3)
        return 'native', 12, {'complete': 12}, 'same'

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(memo.evaluate, ('old-key',), compute)
        assert entered.wait(3)
        memo.forget_context()
        release.set()
        assert future.result(3)[1] == 12
    assert memo._last is None
    assert memo.evaluate(('new-key',), lambda: ('native', 15, {}, 'new'))[1] == 15


def test_uncacheable_input_releases_previous_projection_leaf_without_gc():
    import weakref

    class Leaf(str):
        pass

    _agent, params = _context_case()
    leaf = Leaf('previous complete projection')
    reference = weakref.ref(leaf)
    memo = ContextEstimateMemo()
    params._context_estimate_memo = memo
    memo.remember_prompt_projection(('old-key', 'old-output-key', leaf, ()), revision=memo.context_revision())
    del leaf
    assert reference() is not None
    params.tool_ir_history = [UserTurn('x' * 300_000)]
    pressure.project_request_prompt_history(params, 'next complete prompt')
    assert reference() is None
    assert memo.prompt_projection() is None


def test_large_history_estimate_still_counts_complete_content():
    agent, params = _context_case()
    small = pressure._model_visible_context_components(agent, params, 'prompt')[1]
    params.tool_ir_history = [UserTurn('x' * 300_000 + 'END-OF-COMPLETE-HISTORY')]
    large = pressure._model_visible_context_components(agent, params, 'prompt')[1]
    assert large > small + 50_000
    assert params._context_estimate_memo._last is None
    assert params._context_estimate_memo.prompt_projection() is None


def test_large_projection_forgets_once_and_late_small_projection_cannot_republish(monkeypatch):
    from copy import copy

    from agent_py_agent.agent.agent_core import tool_ir_history

    _agent, params = _context_case()
    memo = ContextEstimateMemo()
    params._context_estimate_memo = memo
    large = copy(params)
    large.tool_ir_history = [UserTurn('x' * 300_000)]
    entered, release = Event(), Event()
    original = tool_ir_history.project_native_prompt_history
    original_forget = memo.forget_context
    forgets = []

    def tracked_forget():
        forgets.append(1)
        original_forget()

    def projected(value, prompt, **kwargs):
        if prompt == 'short complete prompt':
            entered.set()
            assert release.wait(3)
        return original(value, prompt, **kwargs)

    monkeypatch.setattr(memo, 'forget_context', tracked_forget)
    monkeypatch.setattr(tool_ir_history, 'project_native_prompt_history', projected)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(pressure.project_request_prompt_history, params, 'short complete prompt')
        assert entered.wait(3)
        try:
            big_prompt, big_history = pressure.project_request_prompt_history(large, 'large complete prompt')
            assert big_prompt == 'large complete prompt' and len(big_history[0].text) == 300_000
        finally:
            release.set()
        small_prompt, small_history = pending.result(3)
    assert small_prompt == 'short complete prompt' and small_history == params.tool_ir_history
    assert memo.prompt_projection() is None
    assert forgets == [1]
    # 同请求完整key重新核验后，新一代仍可正常发表，未关闭缓存能力。
    pressure.project_request_prompt_history(params, 'short complete prompt')
    assert memo.prompt_projection() is not None


def test_key_accounting_does_not_allocate_temporary_size_tuples(monkeypatch):
    import sys

    source = {'history': [{'role': 'user', 'text': 'complete'}], 'number': float('nan')}
    expected = pressure._context_value_fingerprint(source)
    tuple_queries = []

    def measured(value):
        if type(value) is tuple:
            tuple_queries.append(value)
        return sys.getsizeof(value)

    monkeypatch.setattr(pressure, 'sys', SimpleNamespace(getsizeof=measured))
    assert pressure._context_value_fingerprint(source) == expected
    assert tuple_queries == []


def test_large_estimate_preserves_cacheable_prompt_projection(monkeypatch):
    from agent_py_agent.agent.agent_core import tool_ir_history

    agent, params = _context_case()
    params.provider_history_messages = [{'role': 'user', 'content': 'x' * 300_000}]
    original = tool_ir_history.project_native_prompt_history
    calls = []

    def projected(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(tool_ir_history, 'project_native_prompt_history', projected)
    results = [pressure._model_visible_context_components(agent, params, 'small prompt') for _ in range(3)]
    assert results[0] == results[1] == results[2]
    assert results[0][1] > 50_000
    assert calls == [1]
    assert params._context_estimate_memo.prompt_projection() is not None
    assert params._context_estimate_memo._last is None


def test_estimate_invalidation_fences_only_estimate_producer():
    memo = ContextEstimateMemo()
    revision = memo.context_revision()
    entered, release = Event(), Event()

    def compute():
        entered.set()
        assert release.wait(3)
        return 'native', 12, {}, 'exact'

    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(memo.evaluate, ('estimate',), compute)
        assert entered.wait(3)
        try:
            memo.forget_estimate()
            memo.remember_prompt_projection(('key', 'output-key', 'complete', ()), revision=revision)
            assert memo.prompt_projection() is not None
        finally:
            release.set()
        assert pending.result(3)[1] == 12
    assert memo._last is None


# === Source: agent_py_agent/tests/test_r8_dispatch_log_retention.py ===

def _case_dispatch_log(tmp_path, *, status='done'):
    owner = tmp_path / 'owner'
    owner.mkdir()
    repo = RuntimeRepository(owner / 'runtime.db')
    with closing(sqlite3.connect(repo.db_path)) as conn:
        conn.execute('INSERT INTO task_runs(task_run_id,task_id,status,created_at,updated_at) VALUES(?,?,?,?,?)', ('t', 'task', status, 1, 1))
        conn.execute('INSERT INTO agent_runs(agent_run_id,task_run_id,run_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)', ('a', 't', 'run', status, 1, 1))
        conn.execute('INSERT INTO agent_attempts(attempt_id,agent_run_id,attempt_generation,status,started_at,ended_at) VALUES(?,?,?,?,?,?)', ('attempt', 'a', 1, status, 1, 2))
        conn.commit()
    folder = tmp_path / 'background_dispatch'
    folder.mkdir()
    log = folder / 'launch.log'
    log.write_bytes(b'original recovery evidence')
    record_dispatch_log_exit(log, returncode=0, run_ids=['run'], attempt_ids={'run': 'attempt'}, owner_home=owner,
        expected_identity=(log.stat().st_dev, log.stat().st_ino))
    return owner, folder, log


def test_terminal_log_expires_after_retention_and_exit_metadata_retires(tmp_path):
    owner, folder, log = _case_dispatch_log(tmp_path)
    assert retire_dispatch_logs_slice(folder, owner_home=owner, now=time.time() + 31 * 86400)['deleted'] == 1
    assert not log.exists()
    assert not log.with_suffix('.exit.json').exists()


def test_active_or_unknown_business_preserves_process_exit_evidence(tmp_path):
    owner, folder, log = _case_dispatch_log(tmp_path, status='running')
    assert retire_dispatch_logs_slice(folder, owner_home=owner, now=time.time() + 31 * 86400)['deleted'] == 0
    assert log.exists() and log.with_suffix('.exit.json').exists()
    unknown = folder / 'unknown.log'
    unknown.write_bytes(b'no exit proof')
    assert retire_dispatch_logs_slice(folder, now=time.time() + 1000 * 86400)['deleted'] == 0
    assert unknown.exists()


def test_inherited_stdout_lease_protects_log_after_direct_process_exit(tmp_path):
    owner, folder, log = _case_dispatch_log(tmp_path)
    with log.open('ab') as inherited:
        pin_dispatch_log(inherited)
        assert retire_dispatch_logs_slice(folder, owner_home=owner, now=time.time() + 31 * 86400)['deleted'] == 0
        assert log.exists()
    assert retire_dispatch_logs_slice(folder, owner_home=owner, now=time.time() + 31 * 86400)['deleted'] == 1


def test_log_modified_after_exit_is_preserved(tmp_path):
    _, folder, log = _case_dispatch_log(tmp_path)
    marker = json.loads(log.with_suffix('.exit.json').read_text())
    os.utime(log, (marker['finished_at'] + 1, marker['finished_at'] + 1))
    assert retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)['deleted'] == 0
    assert log.exists()


def test_old_unversioned_attempt_proof_and_unknown_attempt_keep_log(tmp_path):
    owner, folder, log = _case_dispatch_log(tmp_path)
    marker = log.with_suffix('.exit.json')
    row = json.loads(marker.read_text())
    row['schema'] = 'background_dispatch_log_exit.v1'
    marker.write_text(json.dumps(row))
    assert retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)['deleted'] == 0
    row['schema'] = 'background_dispatch_log_exit.v2'
    marker.write_text(json.dumps(row))
    with closing(sqlite3.connect(owner / 'runtime.db')) as conn:
        conn.execute("UPDATE agent_attempts SET status='unknown'")
        conn.commit()
    assert retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)['deleted'] == 0
    assert log.exists()


def test_real_dispatch_exit_proof_uses_original_attempt_and_stdout_inode(tmp_path, monkeypatch):
    import sys
    import threading
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.orchestration.background import dispatch, log_retention
    owner, folder, _log = _case_dispatch_log(tmp_path)
    agent = SimpleNamespace(root=owner, subagents=SimpleNamespace(workspace=tmp_path))
    request = dispatch._BackgroundDispatchRequest(agent=agent, run_ids=['run'], launch_id='actual', router=None, cfg=None,
        params=SimpleNamespace(expected_attempt_ids={'run': 'attempt'}))
    monkeypatch.setattr(dispatch, '_background_dispatch_command', lambda *_args: [sys.executable, '-c', 'from agent_py_agent.agent.io.bounded_process_log import bounded_stdio_from_environment\nwith bounded_stdio_from_environment(): print("actual stdout")'])
    with dispatch._BackgroundProcessWaitOwner(agent, request) as wait_owner:
        process = dispatch._spawn_background_dispatch_process(agent, request, wait_owner=wait_owner)
        dispatch._remember_background_dispatch(agent, 'actual', ['wrong-display-run'], 'display-only')
        settled = threading.Event()
        original = log_retention.record_dispatch_log_exit
        def record(*args, **kwargs):
            original(*args, **kwargs)
            settled.set()
        monkeypatch.setattr(log_retention, 'record_dispatch_log_exit', record)
    assert settled.wait(5)
    marker = folder / 'actual.exit.json'
    row = json.loads(marker.read_text())
    assert row['run_ids'] == ['run'] and row['attempt_ids'] == {'run': 'attempt'}
    assert tuple(row['identity']) == process._my_agent_background_log_identity
    retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)
    assert not (folder / 'actual.log').exists() and not marker.exists()
    assert not (folder / 'actual.capture.json').exists()


def test_unrelated_old_replica_does_not_permanently_hold_current_launch(tmp_path):
    owner, folder, log = _case_dispatch_log(tmp_path)
    data = owner / 'data'
    data.mkdir()
    legacy = data / 'runtime.db'
    RuntimeRepository(legacy)
    with closing(sqlite3.connect(legacy)) as conn:
        for name in ('idx_task_runs_current_task', 'idx_agent_runs_current_run', 'idx_wake_queue_current_task', 'idx_wake_queue_current_run'):
            conn.execute('DROP INDEX ' + name)
    assert retire_dispatch_logs_slice(folder, now=time.time() + 31 * 86400)['deleted'] == 1
    assert not log.exists()


# === Source: agent_py_agent/tests/test_r8_exact_task_readers.py ===

_READERS = (
    "workspace",
    "background_status",
    "background_link",
    "claim_scope",
    "audit_prepare",
    "audit_facts",
    "audit_parent",
)


def _case_exact_task(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "isolated", "channel": "internal"})
    old = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "old"})
    store.tasks.update_status({"task_id": old.task_id, "status": "completed"})
    target = store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-target",
            "work_kind": "audit",
            "work_name": "exact-audit",
            "status": "active",
            "duration_seconds": 86400,
            "cancellation_scope": "detached",
        }
    )
    store.storage.task_path(old.task_id).write_text("{broken", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("exact reader traversed task history")

    for name in ("list_report", "list", "iter_task_ids"):
        monkeypatch.setattr(store.tasks, name, forbidden)
    calls, read = [], store_tasks._read_task_link

    def point(path, task_id, **kwargs):
        calls.append(task_id)
        return read(path, task_id, **kwargs)

    monkeypatch.setattr(store_tasks, "_read_task_link", point)
    return store, thread, target, calls


def _call(reader, store, thread_id, task_id):
    agent = SimpleNamespace(conversation_store=store)
    if reader == "workspace":
        return workspace._conversation_task_link(store, thread_id, task_id)
    if reader == "background_status":
        return runtime._background_task_link_status(
            agent, SimpleNamespace(thread_id=thread_id, task_id=task_id)
        )
    if reader == "background_link":
        return runtime._background_conversation_task_link(agent, thread_id, task_id)
    if reader == "claim_scope":
        return runtime._background_claim_scope_id(store, thread_id, task_id)
    if reader == "audit_prepare":
        return completion._matching_audit_prepare_link(store, thread_id, task_id, "exact-audit")
    if reader == "audit_facts":
        params = SimpleNamespace(
            task_id=task_id,
            task_attributes={
                "conversation_thread_id": thread_id,
                "conversation_task_id": task_id,
                completion.CONVERSATION_WORK_KIND_ATTR: "audit",
            },
        )
        return completion._active_named_audit_facts(agent, params)
    return audit_parent_reconcile_state(agent, task_id)


def _unavailable(reader, *, missing=False):
    if reader == "audit_parent":
        return False, "inactive" if missing else "unavailable"
    if reader == "audit_facts":
        return (
            {}
            if missing
            else {
                "task_id": "audit-target",
                "work_kind": "audit",
                "state_available": False,
                "continues_without_more_user_input": True,
            }
        )
    return "" if reader in {"background_status", "claim_scope"} else None


@pytest.mark.parametrize("reader", _READERS)
def test_exact_task_readers_ignore_unrelated_history_and_only_load_target(
    tmp_path, monkeypatch, reader
):
    store, thread, target, calls = _case_exact_task(tmp_path, monkeypatch)
    result = _call(reader, store, thread.thread_id, target.task_id)
    if reader == "background_status":
        assert result == "active"
    elif reader == "claim_scope":
        assert result == detached_task_claim_scope_id(thread.thread_id, target.task_id)
    elif reader == "audit_facts":
        assert result["state_available"] is True and result["task_id"] == target.task_id
    elif reader == "audit_parent":
        assert result == (True, "active")
    else:
        assert result == target
    assert calls and set(calls) == {target.task_id} and len(calls) <= 2


@pytest.mark.parametrize("reader", _READERS)
@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_exact_task_readers_keep_missing_and_corrupt_boundaries(
    tmp_path, monkeypatch, reader, fault
):
    store, thread, target, _calls = _case_exact_task(tmp_path, monkeypatch)
    path = store.storage.task_path(target.task_id)
    if fault == "missing":
        path.unlink()
    else:
        path.write_text("{broken target", encoding="utf-8")
    assert _call(reader, store, thread.thread_id, target.task_id) == _unavailable(
        reader, missing=fault == "missing"
    )


@pytest.mark.parametrize("reader", _READERS[:-1])
def test_exact_task_readers_reject_foreign_thread(tmp_path, monkeypatch, reader):
    store, _thread, target, _calls = _case_exact_task(tmp_path, monkeypatch)
    other = store.threads.create(canonical_user_id="other")
    assert _call(reader, store, other.thread_id, target.task_id) == _unavailable(
        reader, missing=True
    )


_ACTIVE_READERS = ("named_kind", "named_select", "exact_audit", "audit_conflict")


def _active_call(reader, store, thread_id, target):
    from agent_py_agent.agent.conversation import audit_lifecycle, named_work

    if reader == "named_kind":
        return named_work._resolve_named_work_kind(store, thread_id, target.work_name)
    if reader == "named_select":
        return named_work._select_named_audit(store, thread_id, target.work_name)
    if reader == "exact_audit":
        return audit_lifecycle.resolve_exact_active_audit(store, thread_id, target.work_name)
    return store.audits._active_named_work_conflict(target, target.task_id)


@pytest.mark.parametrize("reader", _ACTIVE_READERS)
def test_active_named_readers_exclude_terminal_history(tmp_path, monkeypatch, reader):
    store, thread, target, calls = _case_exact_task(tmp_path, monkeypatch)
    result = _active_call(reader, store, thread.thread_id, target)
    expected = {
        "named_kind": ("audit", ""),
        "named_select": (target.task_id, ""),
        "exact_audit": target,
        "audit_conflict": False,
    }
    assert result == expected[reader]
    assert calls == [target.task_id]


@pytest.mark.parametrize("reader", _ACTIVE_READERS)
def test_active_named_readers_reject_corrupt_active_link(tmp_path, monkeypatch, reader):
    from agent_py_agent.agent.conversation.audit_lifecycle import AuditLifecycleError
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    store, thread, target, _calls = _case_exact_task(tmp_path, monkeypatch)
    store.storage.task_path(target.task_id).write_text("{broken", encoding="utf-8")
    if reader in {"named_kind", "named_select"}:
        assert _active_call(reader, store, thread.thread_id, target) == (
            "",
            "CONVERSATION_TASK_STATE_UNAVAILABLE",
        )
    else:
        with pytest.raises(AuditLifecycleError if reader == "exact_audit" else DataCorruptionError):
            _active_call(reader, store, thread.thread_id, target)


# === Source: agent_py_agent/tests/test_r8_fts_cold_discovery.py ===

def _current_workspaces(tmp_path, count):
    home = ensure_my_agent_home(tmp_path / 'host')
    template = LocalStore(tmp_path / 'template' / 'local.db')
    template.rebuild_fts()
    for _ in range(20):
        if not template.cleanup_fts_retired_step(max_rows=128, max_bytes=1024*1024, max_ms=1000)['pending']:
            break
    for i in range(count):
        path = home.owner_home_dir / 'workspace/runtime/workspaces' / f'scope-{i}' / 'local_store/local.db'
        path.parent.mkdir(parents=True)
        shutil.copyfile(template.db_path, path)
        path.chmod(0o600)
    return home


def _names(home):
    names, cursor = [], {}
    while True:
        page, cursor, complete = directory_page(home.owner_home_dir, upgrade._WORKSPACES, cursor, 64, require_dir_fd=False)
        names.extend(page)
        if complete:
            return names


def _restart(monkeypatch):
    monkeypatch.setattr(upgrade, '_HOT_HINTS', (os.getpid(), Lock(), OrderedDict()))


def test_cold_restart_finds_same_old_map_job_among_two_hundred_current_stores(tmp_path, monkeypatch):
    home = _current_workspaces(tmp_path, 200)
    target = _names(home)[159]
    store = LocalStore(home.owner_home_dir / 'workspace/runtime/workspaces' / target / 'local_store/local.db')
    items = [store.upsert_record(source_type='memory', source_id=str(i), title=str(i), content='coldrecoverneedle') for i in range(8)]
    with store._connection() as conn:
        conn.execute('UPDATE records SET rowid=rowid+100')
        conn.execute("DELETE FROM metadata WHERE key IN ('records_fts_rowid_aligned','records_fts_rebuild_pending')")
        conn.commit()
    for step in range(100):
        store.rebuild_fts_step(max_rows=1, max_bytes=1024*1024, max_ms=1000, start_new=step == 0)
        with store._connection() as conn:
            job = maintenance._fts_job(conn)
        if job['phase'] == 'map':
            break
    assert job['phase'] == 'map' and job['copied'] == len(items)
    identity = job['identity']
    policy = json.loads(home.owner_retention_json.read_text())
    policy['maintenance_enabled'] = False
    home.owner_retention_json.write_text(json.dumps(policy))
    clock = SimpleNamespace(now=time.monotonic())
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: clock.now
    monkeypatch.setattr(upgrade, 'time', proxy)
    found = False
    for turn in range(20):
        _restart(monkeypatch)  # 提示丢失/重启，只有原维护游标与SQLite任务仍在。
        clock.now = time.monotonic()
        run_owner_retention_if_due(home, now=1000+61*turn)
        saved = json.loads((home.owner_data_dir / 'maintenance.json').read_text())['local_store_fts_upgrade']
        if saved.get('store') == store.db_path.relative_to(home.owner_home_dir).as_posix():
            found = True
            break
    assert found, '160个候选应在最多20个原维护机会被发现，而非等160分钟'
    monkeypatch.setattr(upgrade, 'time', time)
    for extra in range(8):
        _restart(monkeypatch)
        run_owner_retention_if_due(home, now=1000+61*(turn+extra+1))
        with store._connection() as conn:
            resumed = maintenance._fts_job(conn)
        if resumed['generation'] > job['generation']:
            break
    assert resumed['generation'] > job['generation']
    assert resumed['identity'] == identity
    assert resumed['copied'] >= len(items)
    assert resumed['phase'] in {'map', 'publish', 'complete'}
    # 全文沿原索引仍可查；本测试没有业务读取制造热提示来驱动发现。
    assert {row.id for row in store._search_fts('coldrecoverneedle', limit=20, source_type=None, visibility=None)} == {row.id for row in items}


def test_current_only_discovery_is_eight_pages_and_stops_at_complete_directory(tmp_path, monkeypatch):
    home = _current_workspaces(tmp_path, 20)
    _restart(monkeypatch)
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: 1000.0
    monkeypatch.setattr(upgrade, 'time', proxy)
    calls = []
    from agent_py_agent.agent.io import sqlite_scope
    connections = []
    connect = sqlite_scope.connect_projection_sqlite

    def opened(*args, **kwargs):
        connections.append(args[0])
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite_scope, 'connect_projection_sqlite', opened)
    original = upgrade.directory_page

    def page(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(upgrade, 'directory_page', page)
    previous = {}
    sizes = []
    for turn in range(3):
        before = len(calls)
        with upgrade.fts_upgrade_tick_scope(deadline=1002, max_steps=50):
            report = upgrade.advance_upgrade(home.owner_home_dir, previous, 1000+61*turn)
        sizes.append(len(calls)-before)
        previous = {'local_store_fts_upgrade': report}
    assert sizes == [8, 8, 5]  # 最后一页是原生EOF，不回绕扫描第一库。
    assert report['discovery_complete']
    assert len(connections) == 20


def test_slow_first_cold_page_does_not_receive_another_slice(tmp_path, monkeypatch):
    home = _current_workspaces(tmp_path, 20)
    _restart(monkeypatch)
    clock = SimpleNamespace(now=1000.0)
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: clock.now
    monkeypatch.setattr(upgrade, 'time', proxy)
    original = upgrade.directory_page
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        result = original(*args, **kwargs)
        clock.now += .030
        return result

    monkeypatch.setattr(upgrade, 'directory_page', slow)
    result = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert result['status'] == 'budget_exhausted' and calls == [1]
    assert len(result['cursor']['pending']) == 1
    assert result['catchup']['steps'] == 1


def test_multiple_owners_share_global_steps_and_cold_deadline(tmp_path, monkeypatch):
    first = _current_workspaces(tmp_path / 'first', 20)
    second = _current_workspaces(tmp_path / 'second', 20)
    _restart(monkeypatch)
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: 1000.0
    monkeypatch.setattr(upgrade, 'time', proxy)
    deadlines = []
    original = upgrade._advance_upgrade_slice

    def observe(*args, **kwargs):
        deadlines.append(kwargs['deadline'])
        return original(*args, **kwargs)

    monkeypatch.setattr(upgrade, '_advance_upgrade_slice', observe)
    with upgrade.fts_upgrade_tick_scope(deadline=1002, max_steps=10) as budget:
        a = upgrade.advance_upgrade(first.owner_home_dir, {}, 1000)
        b = upgrade.advance_upgrade(second.owner_home_dir, {}, 1000)
        assert upgrade.advance_upgrade(first.owner_home_dir, {}, 1000) is None
    assert a['catchup']['cold_pages'] == 8 and b['catchup']['cold_pages'] == 2
    assert budget.remaining_steps == 0
    assert len(deadlines) == 10 and set(deadlines) == {1000.025}


def test_unknown_database_or_no_cursor_progress_yields_without_more_cold_io(tmp_path, monkeypatch):
    home = _current_workspaces(tmp_path, 20)
    _restart(monkeypatch)
    first = _names(home)[0]
    path = home.owner_home_dir / 'workspace/runtime/workspaces' / first / 'local_store/local.db'
    path.write_bytes(b'not a sqlite database')
    report = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert report['status'] == 'unknown' and report['catchup']['steps'] == 1
    monkeypatch.setattr(upgrade, 'directory_page', lambda *args, **kwargs: ([], {}, False))
    previous = {'local_store_fts_upgrade': {'cursor': {'fixed': 3}, 'next_at': 0}}
    report = upgrade.advance_upgrade(home.owner_home_dir, previous, 1061)
    assert report['status'] == 'discovery' and report['catchup']['steps'] == 1
    assert not report['discovery_advanced']


def test_empty_workspace_discovery_uses_eight_pages_without_sqlite(tmp_path, monkeypatch):
    from agent_py_agent.agent.io import sqlite_scope

    home = ensure_my_agent_home(tmp_path)
    workspaces = home.owner_home_dir.joinpath(*upgrade._WORKSPACES)
    for i in range(200):
        (workspaces / f'expired-{i}').mkdir(parents=True)
    _restart(monkeypatch)
    proxy = SimpleNamespace(**vars(time))
    proxy.monotonic = lambda: 1000.0
    monkeypatch.setattr(upgrade, 'time', proxy)

    def forbidden(*args, **kwargs):
        raise AssertionError('empty workspace must not open SQLite')

    monkeypatch.setattr(sqlite_scope, 'connect_projection_sqlite', forbidden)
    result = upgrade.advance_upgrade(home.owner_home_dir, {}, 1000)
    assert result['status'] == 'discovery'
    assert result['catchup']['cold_pages'] == 8
    assert result['catchup']['steps'] == 8


@pytest.mark.parametrize('selection', ['hot', 'pending'])
def test_current_hot_or_pending_slot_does_not_borrow_cold_pages(tmp_path, monkeypatch, selection):
    home = _current_workspaces(tmp_path, 20)
    _restart(monkeypatch)
    name = _names(home)[0]
    relative = f'workspace/runtime/workspaces/{name}/local_store/local.db'
    previous = {}
    if selection == 'hot':
        upgrade.note_active_store(home.owner_home_dir / relative)
    else:
        previous = {'local_store_fts_upgrade': {'cursor': {'pending': [relative], 'discover_next': False}}}

    def forbidden(*args, **kwargs):
        pytest.fail('hot/pending opportunity must not start a cold discovery burst')

    monkeypatch.setattr(upgrade, 'directory_page', forbidden)
    report = upgrade.advance_upgrade(home.owner_home_dir, previous, 1000)
    assert report['selection'] == selection and report['status'] == 'current'
    assert report['catchup']['steps'] == 1 and report['catchup']['cold_pages'] == 0


# === Source: agent_py_agent/tests/test_r8_idle_claim_history.py ===

def test_history_is_scanned_in_slices_and_orphan_active_claim_is_preserved(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversation')
    thread = store.threads.create()
    directory = store.storage.background_claims_dir
    for number in range(500):
        write(directory / f'{number}.json', {'status': 'finished', 'thread_id': thread.thread_id})
    read = view._read_claim_report
    reads = []
    monkeypatch.setattr(view, '_read_claim_report', lambda path: (reads.append(path), read(path))[1])
    for call in range(100):
        reads.clear()
        state = view.claim_activity_state(store.storage, thread.thread_id)
        assert len(reads) <= 128
        if not state:
            break
        assert state == 'activity_unknown'
    else:
        raise AssertionError('静止历史必须最终核对完成')
    assert call >= 3
    reads.clear()
    assert view.claim_activity_state(store.storage, thread.thread_id) == ''
    assert reads == []
    # 没有 TaskLink 的物理租约也属于未退出执行体，不能被最近任务窗口隐藏。
    write(directory / '499.json', {'status': 'running', 'thread_id': thread.thread_id})
    for _ in range(100):
        state = view.claim_activity_state(store.storage, thread.thread_id)
        assert state
        if state == 'foreground_active':
            break
    else:
        raise AssertionError('遗漏孤立旧 claim')


def test_atomic_claim_change_during_scan_never_proves_idle(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / 'conversation')
    thread = store.threads.create()
    path = store.storage.background_claims_dir / 'old.json'
    write(path, {'status': 'finished', 'thread_id': thread.thread_id})
    read_started, rewritten = threading.Event(), threading.Event()
    original = view._read_claim_report

    def read(candidate):
        snapshot = original(candidate)
        read_started.set()
        assert rewritten.wait(2)
        return snapshot

    def change():
        assert read_started.wait(2)
        write(path, {'status': 'running', 'thread_id': thread.thread_id})
        rewritten.set()

    monkeypatch.setattr(view, '_read_claim_report', read)
    writer = threading.Thread(target=change)
    writer.start()
    try:
        assert view.claim_activity_state(store.storage, thread.thread_id) == 'activity_unknown'
    finally:
        writer.join(2)
    assert not writer.is_alive()
    monkeypatch.setattr(view, '_read_claim_report', original)
    assert view.claim_activity_state(store.storage, thread.thread_id) == 'foreground_active'
