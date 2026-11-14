"""Wave 3 lossless consolidated tests (test_consolidated_c5_g3).

Sources:
- test_r8_usage_source_maintenance.py: 真实维护入口和跨进程目录续点，查询不得替后台补建索引。
- test_r9_background_observation_bounds.py: 已结束但未结片保责任；日常复核公平有界，已退 worker 不再保正文引用。
- test_r9_candidate_promotion_windows.py: 只读窗口的规模、跨重启公平、旧JSONL续点与零提取续办。
- test_r9_feishu_user_event_sources.py: 指定成员只沿真实订阅、官方事实和原持久观察入口；不发模型请求。
- test_rework_promotion_item_isolation.py: R4-P1-04：一条确定性失败的候选不能拖垮同批其它候选的正式晋升。
- test_rework_r10_child_lineage_authority.py: R10 返工 R1：子代理创建的模型建议与谱系权威（P1-R10-01/02），全程真实 SubAgentManager、RuntimeDB 与 ConversationStore。
- test_rework_r1_event_maintenance.py: Rework R1 / GPT-P1-01: bounded event maintenance keeps a resumable cursor across normal appends.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.adapter import (
    channel_observations as transport,
)
from agent_py_agent.agent.adapter import (
    feishu_provider_facts as facts,
)
from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.common.owner_write_errors import OwnerManagementIngressUnavailable
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
)
from agent_py_agent.agent.conversation import (
    ConversationStore,
)
from agent_py_agent.agent.conversation import (
    background_shutdown as shutdown,
)
from agent_py_agent.agent.conversation.host_message_context import media_for_run
from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.conversation.models import ThreadModelUsageEvent
from agent_py_agent.agent.conversation.usage_source_maintenance import (
    maintain_usage_sources_lane,
)
from agent_py_agent.agent.gateway_parts import channel_observations as endpoint
from agent_py_agent.agent.local_storage import LocalStore, maintenance
from agent_py_agent.agent.memory_store import candidate_ledger, promotion
from agent_py_agent.agent.memory_store.candidate_models import (
    CandidateObservation,
    MemoryScope,
)
from agent_py_agent.agent.memory_store.curator_lease import release_curator_lease
from agent_py_agent.agent.memory_store.curator_state import CuratorLeaseRequest
from agent_py_agent.agent.scheduler.event_sources import normalize_event_schedule
from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
)
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    _child_create_params,
    scheduled_child_create_params,
)
from agent_py_agent.agent.user_space.directory_paging import directory_page
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import (
    _maintain_event_store,
    run_owner_storage_maintenance_slice,
)
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder
from agent_py_agent.tests.test_decision_subagent import created, provider
from agent_py_agent.tests.test_decision_subagent import prepared as prepared
from agent_py_agent.tests.test_feishu_bot_observations import (
    adapter,
    host,
    payload,
    provider_read,
)
from agent_py_agent.tests.test_host_message_context import select_child
from agent_py_agent.tests.test_im_input_media_delegation import active_media_child, media_case
from agent_py_agent.tests.test_memory_curator_v2 import _service, _StaticStructuredBackend
from agent_py_agent.tests.test_memory_promotion_v2 import _explicit, _runtime
from agent_py_agent.tests.test_rework_r2_curator_batch import _agent, _curator, _thread
from agent_py_agent.tests.test_scheduler_event_sources import event, schedule

# === Source: agent_py_agent/tests/test_r8_usage_source_maintenance.py ===

@pytest.mark.skipif(os.name == 'nt', reason='原生目录FD续点只适用于POSIX维护通道')
def test_directory_checkpoint_survives_process_restart_with_small_receipt(tmp_path):
    for number in range(260):
        (tmp_path / (f'{number:04}-' + 'x' * 180)).mkdir()
    first, cursor, done = directory_page(tmp_path, (), {}, 1)
    assert not done and len(first) == 1
    assert len(json.dumps(cursor)) < 8192
    script = '''import json,sys
from pathlib import Path
from agent_py_agent.agent.user_space.directory_paging import directory_page
print(json.dumps(directory_page(Path(sys.argv[1]), (), json.loads(sys.argv[2]), 1)))
'''
    raw = subprocess.check_output([sys.executable, '-c', script, str(tmp_path), json.dumps(cursor)],
                                  cwd=Path(__file__).resolve().parents[2], text=True, timeout=30)
    second, cursor, done = json.loads(raw)
    assert second and first != second
    found = {*first, *second}
    while not done:
        names, cursor, done = directory_page(tmp_path, (), json.loads(json.dumps(cursor)), 7)
        assert len(names) <= 7 and len(json.dumps(cursor)) < 8192
        assert not found.intersection(names)
        found.update(names)
    assert len(found) == 260


@pytest.mark.skipif(os.name == 'nt', reason='原生owner维护目录FD合同为POSIX')
def test_real_owner_lane_backfills_cold_usage_without_new_appends(tmp_path):
    home = ensure_my_agent_home(tmp_path / 'host')
    root = home.owner_workspace_dir / 'runtime/workspaces/old-project/conversations'
    store = ConversationStore(root)
    thread = store.threads.get_or_create({'canonical_user_id': home.owner_id, 'channel': 'internal'})
    path = store.storage.model_usage_dir / f'{thread.thread_id}.jsonl'
    rows = [ThreadModelUsageEvent(f'event-{i}', thread.thread_id, f'request-{i}', '', '', 'test',
                                  {'schema': 'model_call_summary.v1', 'total_tokens': 1}, 1000).to_dict()
            for i in range(350)]
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    before = path.read_bytes()
    retention = SimpleNamespace(applied=True, legal_hold=False, load_errors=[])
    previous = {'storage_maintenance': {'continuation_pending': True, 'pending_kinds': ['usage_sources']}}
    receipts = []
    for _ in range(100):
        state = run_owner_storage_maintenance_slice(home, previous, retention, now=time.time(),
                                                    quota_max_ms=10, quota_max_entries=128)
        assert not state['errors']
        receipt = state['lane_reports'].get('usage_sources')
        if receipt:
            receipts.append(receipt)
            assert receipt['items'] <= 4
        if 'usage_sources' not in state['pending_kinds']:
            break
        previous = {'storage_maintenance': state}
    else:
        pytest.fail('有界owner维护应最终追平旧用量，不等待新的模型消费')
    assert receipts and not any(receipt.get('errors') for receipt in receipts)
    fresh = ConversationStore(root, initialize=False)
    assert [identifier for identifier, _ in fresh.model_usage.window_source_plan(since=999, until=1001).rows()] == [thread.thread_id]
    events, errors = fresh.model_usage.events_window_report(thread.thread_id, since=999, until=1001)
    assert not errors and len(list(events)) == 350
    assert path.read_bytes() == before


@pytest.mark.parametrize('held', [True, False])
def test_hold_or_exhausted_budget_does_not_create_projection(tmp_path, held):
    home = ensure_my_agent_home(tmp_path / 'host')
    root = home.owner_home_dir / 'conversations'
    root.mkdir(exist_ok=True)
    (root / 'model_usage').mkdir(exist_ok=True)
    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    request = StorageLaneRequest('usage_sources', home, 1, {}, StorageLaneBudget(4 if held else 0, 8192,
                                time.monotonic() + 1), held, False, None, None)
    result = validate_lane_receipt(maintain_usage_sources_lane(request))
    assert result['items'] == 0
    assert sorted(str(path.relative_to(root)) for path in root.rglob('*')) == before


@pytest.mark.skipif(os.name == 'nt', reason='原生owner维护目录FD合同为POSIX')
def test_missing_usage_directories_do_not_enter_write_lane(tmp_path):
    home = ensure_my_agent_home(tmp_path / 'host')
    root = home.owner_workspace_dir / 'runtime/workspaces/empty-project/conversations'
    root.mkdir(parents=True)
    before = sorted(str(path.relative_to(home.owner_home_dir)) for path in home.owner_home_dir.rglob('*'))
    commits, cursor = [], {}
    def commit(operation):
        commits.append(operation)
        return operation()
    for _ in range(8):
        request = StorageLaneRequest('usage_sources', home, time.time(), cursor,
                                    StorageLaneBudget(4, 8192, time.monotonic() + 1), False, True, None, commit)
        result = validate_lane_receipt(maintain_usage_sources_lane(request))
        assert not result.get('errors')
        cursor = result['cursor']
        if result['complete']:
            break
    else:
        pytest.fail('empty usage source discovery did not finish')
    assert commits == []
    assert sorted(str(path.relative_to(home.owner_home_dir)) for path in home.owner_home_dir.rglob('*')) == before


# === Source: agent_py_agent/tests/test_r9_background_observation_bounds.py ===

class CountingRepo:
    calls = 0
    pending = True

    def __init__(self, *args, **kwargs):
        self.db_path = 'isolated-counting-repository'

    def get_attempt(self, attempt_id):
        type(self).calls += 1
        return {'agent_run_id': 'agent', 'status': 'done', 'ended_at': 1}

    def get_agent_run(self, agent_run_id):
        return {'task_run_id': 'task-run', 'current_attempt_id': 'attempt', 'run_id': 'run', 'status': 'done'}

    def get_task_run(self, task_run_id):
        return {'task_id': 'task', 'status': 'done'}

    def unsettled_attempt_operations(self, attempt_id):
        return [{'status': 'UNKNOWN', 'operation_id': 'exact-operation'}] if self.pending else []

    def get_agent_run_exec_lock(self, agent_run_id):
        return None


def claim(index):
    row = {'schema_version': 'background_run_claim.v1', 'claim_id': f'claim-{index}',
           'thread_id': f'thread-{index}', 'status': 'finished',
           'runtime_authority': {'schema_version': 'conversation_claim_execution.v1', 'claim_id': f'claim-{index}',
                                 'attempt_id': 'attempt', 'agent_run_id': 'agent', 'task_id': 'task', 'run_id': 'run'}}
    return SimpleNamespace(claim_id=row['claim_id'], thread_id=row['thread_id'], claim_scope_id='',
                           claims=SimpleNamespace(load=lambda *a, **kw: dict(row)))


def seed(control, count, monkeypatch):
    monkeypatch.setattr(shutdown, 'RuntimeRepository', CountingRepo)
    monkeypatch.setattr(CountingRepo, 'pending', True)
    for index in range(count):
        with control.observe(CountingRepo()):
            shutdown.observe_background_claim(claim(index))
    CountingRepo.calls = 0


def test_new_observe_rechecks_at_most_eight_of_257_unknown_slices(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 257, monkeypatch)
    for _ in range(3):
        before = CountingRepo.calls
        with control.observe(None):
            pass
        assert CountingRepo.calls - before <= 8
    assert len(control.slices) == 257
    before = CountingRepo.calls
    blockers = control.blockers()
    assert CountingRepo.calls - before == 257
    assert len(blockers) == 257
    assert {row['reason'] for row in blockers} == {'background_operation_unsettled'}


def test_fair_recheck_reaches_every_unknown_slice_without_discarding_any(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 257, monkeypatch)
    visited = set()
    original = shutdown.BackgroundSlice._claim_blocker
    def count(item, reference):
        visited.add(reference.claim_id)
        return original(item, reference)
    monkeypatch.setattr(shutdown.BackgroundSlice, '_claim_blocker', count)
    for _ in range(33):
        with control.observe(None):
            pass
    assert len(visited) == 257
    assert len(control.slices) == 257


def test_exited_worker_reference_released_but_unknown_claim_still_blocks(monkeypatch):
    control = shutdown.BackgroundShutdown()
    monkeypatch.setattr(shutdown, 'RuntimeRepository', CountingRepo)
    release = threading.Event()
    class Payload:
        pass
    payload = Payload()
    worker = threading.Thread(target=release.wait)
    worker.payload = payload
    payload_ref, worker_ref = weakref.ref(payload), weakref.ref(worker)
    with control.observe(CountingRepo()):
        shutdown.observe_background_claim(claim(0))
        shutdown.start_background_model_worker(worker)
    item, = control.slices
    assert len(item.workers) == 1
    assert any(row['reason'] == 'background_model_worker_unsettled' for row in item.blockers())
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    del worker, payload
    assert worker_ref() is not None
    assert item.blockers() == [{'reason': 'background_operation_unsettled', 'ref': 'claim-0'}]
    assert item.workers == []
    assert item.exited_worker_count == 1
    assert worker_ref() is None and payload_ref() is None
    assert item in control.slices
    assert item.blockers() == [{'reason': 'background_operation_unsettled', 'ref': 'claim-0'}]
    assert item.exited_worker_count == 1


def test_recheck_exception_keeps_entire_reserved_batch_for_later(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 17, monkeypatch)
    first = next(iter(control._finished_rechecks))
    original = first.blockers
    error = OSError('isolated exact source')
    def fail():
        raise error
    monkeypatch.setattr(first, 'blockers', fail)
    with pytest.raises(OSError) as caught:
        with control.observe(None):
            pass
    assert caught.value is error
    assert len(control._finished_rechecks) == len(control.slices) == 17
    monkeypatch.setattr(first, 'blockers', original)
    monkeypatch.setattr(CountingRepo, 'pending', False)
    for _ in range(3):
        with control.observe(None):
            pass
    assert not control.slices and not control._finished_rechecks


def test_concurrent_births_reserve_different_batches_without_losing_unknown(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 17, monkeypatch)
    first = next(iter(control._finished_rechecks))
    original = first.blockers
    entered, release = threading.Event(), threading.Event()
    def held():
        entered.set()
        assert release.wait(5)
        return original()
    monkeypatch.setattr(first, 'blockers', held)
    errors = []
    def birth():
        try:
            with control.observe(None):
                pass
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=birth)
    worker.start()
    try:
        assert entered.wait(5)
        # 第一个出生已预约 8 片；第二个出生须独立推进，而非重复读同一批。
        before = CountingRepo.calls
        with control.observe(None):
            pass
        assert CountingRepo.calls - before == 8
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert len(control.slices) == len(control._finished_rechecks) == 17
    assert len(control.blockers()) == 17


def test_worker_identity_uncertainty_never_releases_reference(monkeypatch):
    control = shutdown.BackgroundShutdown()
    seed(control, 0, monkeypatch)
    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    with control.observe(CountingRepo()):
        shutdown.observe_background_claim(claim(0))
        shutdown.start_background_model_worker(worker)
    item, = control.slices
    release.set()
    worker.join(5)
    original_started = worker._started
    worker._started = threading.Event()
    try:
        assert any(row['reason'] == 'background_model_worker_unsettled' for row in item.blockers())
        assert len(item.workers) == 1 and item.exited_worker_count == 0
    finally:
        worker._started = original_started
    assert item.blockers() == [{'reason': 'background_operation_unsettled', 'ref': 'claim-0'}]
    assert not item.workers and item.exited_worker_count == 1


# === Source: agent_py_agent/tests/test_r9_candidate_promotion_windows.py ===

def seeded(root, count=257):
    service = _service(root, _StaticStructuredBackend({}), ConversationStore(root / 'c'))
    service.candidate_service.observe_many([CandidateObservation(candidate_type='long_term_fact',
        content=f'合成规范候选{i}', subject_key=f'synthetic.{i}', scope=MemoryScope('personal', 'personal'),
        origin='user_explicit', promotion_target='long_term', promotion_mode='auto_eligible',
        learning_source=user_dialogue_projection(),
        source_message_refs=({'thread_id': 'source-thread', 'message_id': f'm-{i}'},)) for i in range(count)])
    service.promotion_callback = lambda candidate_id: None
    return service


def select_under_lease(service, now=None):
    current = now or datetime.now(timezone.utc)
    acquired = service.committer.recover_and_acquire(CuratorLeaseRequest(
        'interval', service.config.revision(), 30, current, purpose='candidate_scan'))
    assert acquired is not None
    lease_id = acquired[1]['lease_id']
    try:
        return service._pending_promotable_candidate_ids(limit=32, lease_id=lease_id, scanned_at=current.isoformat())
    finally:
        release_curator_lease(service.state_store, lease_id)


@pytest.mark.parametrize('count', [85, 257])
def test_every_deferred_candidate_including_last_page_is_served_after_cold_restart(tmp_path, count):
    service = seeded(tmp_path, count)
    served = []
    for _ in range(20):
        identifiers = select_under_lease(service)
        assert len(identifiers) <= 32
        served.extend(identifiers)
        state = service.state_store.load()
        assert not state.pending_reasons
        assert len(state.candidate_scan_cursor.get('pending', [])) <= 128
        if not state.candidate_scan_cursor['active']:
            break
        service = _service(tmp_path, _StaticStructuredBackend({}), ConversationStore(tmp_path / 'c'))
        service.promotion_callback = lambda candidate_id: None
    else:
        pytest.fail('bounded candidate rotation did not finish')
    assert len(served) == len(set(served)) == count
    assert service.state_store.load().candidate_scan_cursor['scan']['wrapped'] is True


def test_indexed_first_window_never_loads_entire_status_group(tmp_path, monkeypatch):
    service = seeded(tmp_path, 513)
    loaded = []
    original = candidate_ledger._read_candidate_row
    def read(*args, **kwargs):
        loaded.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(candidate_ledger, '_read_candidate_row', read)
    monkeypatch.setattr(service.candidate_service, 'readonly_rows', lambda **kwargs: pytest.fail('full group scan'))
    assert len(service._pending_promotable_candidate_ids()) == 32
    assert len(loaded) == 128


def test_legacy_blank_lines_final_no_newline_and_multi_megabyte_window_resume(tmp_path):
    service = seeded(tmp_path, 12)
    rows = service.candidate_service.list()
    records = [row.to_record() for row in rows]
    path = service.candidate_service.path
    path.write_text('\n' + '\n\n'.join(json.dumps(row, ensure_ascii=False).replace('{', '{' + ' ' * 800000, 1) for row in records), encoding='utf-8')
    time.sleep(2.05)  # 真实mtime/ctime/ChangeTime越过原粗时钟窗口后才复用字节续点。
    first, refs, cursor, wrapped = service.candidate_service.readonly_window()
    assert first and not wrapped and 0 < cursor['offset'] < path.stat().st_size
    second, _, _, wrapped = service.candidate_service.readonly_window(cursor)
    assert wrapped and len(first) + len(second) == len(rows)
    assert len({row.candidate_id for row in (*first, *second)}) == len(rows)
    before = cursor
    path.write_text(path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
    restarted, _, after, _ = service.candidate_service.readonly_window(before)
    assert restarted[0].candidate_id == rows[0].candidate_id
    assert after['source'] != before['source']


def test_maintenance_continuation_has_no_extraction_no_trigger_requeue_and_obeys_cooldown(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch
    service = seeded(tmp_path, 85)
    now = datetime.now(timezone.utc).replace(hour=12)
    state = replace(service.state_store.load(), last_daily_finalize_date=now.date().isoformat())
    with service.state_store.path.open('w', encoding='utf-8') as handle:
        json.dump(state.to_dict(), handle)
    assert len(select_under_lease(service, now)) == 32
    monkeypatch.setattr(service, '_collect', lambda *args, **kwargs: CuratorInputBatch(messages=(), audit_events=()))
    monkeypatch.setattr(service, '_promote_eligible', lambda ids: (0, (), ()))
    assert service.run_if_due(now=now + timedelta(seconds=1)).status == 'not_due'
    assert service.run_if_due(now=now + timedelta(seconds=61)).status == 'succeeded'
    assert not service.state_store.load().pending_reasons
    assert service.run_if_due(now=now + timedelta(seconds=62)).status == 'not_due'
    assert service.run_if_due(now=now + timedelta(seconds=122)).status == 'succeeded'
    state = service.state_store.load()
    assert not state.candidate_scan_cursor['active'] and not state.pending_reasons
    assert state.last_daily_finalize_date == now.date().isoformat()
    assert service.backend.calls == 0
    assert service.run_if_due(now=now + timedelta(seconds=183)).status == 'not_due'


@pytest.mark.parametrize('manual,callback', [(True, True), (False, False)])
def test_disabled_promotion_never_creates_backlog_scan(tmp_path, monkeypatch, manual, callback):
    from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch
    service = seeded(tmp_path, 85)
    now = datetime.now(timezone.utc)
    state = replace(service.state_store.load(), last_daily_finalize_date=now.date().isoformat())
    service.state_store.path.write_text(json.dumps(state.to_dict()), encoding='utf-8')
    if manual:
        service.config = replace(service.config, auto_promotion_policy='manual_only')
    if not callback:
        service.promotion_callback = None
    monkeypatch.setattr(service, '_collect', lambda *args, **kwargs: CuratorInputBatch(messages=(), audit_events=()))
    monkeypatch.setattr(service.candidate_service, 'readonly_window', lambda *args, **kwargs: pytest.fail('disabled scan'))
    assert service.run_if_due(now=now).status == 'not_due'
    assert service.state_store.load().candidate_scan_cursor == {}
    assert service.backend.calls == 0


def test_initial_backlog_without_new_messages_is_discovered_and_foreground_still_defers(tmp_path, monkeypatch):
    from agent_py_agent.agent.backends import request_scope
    from agent_py_agent.agent.memory_store.curator_due import candidate_scan_deadline
    service = seeded(tmp_path, 85)
    now = datetime.now(timezone.utc)
    state = replace(service.state_store.load(), last_daily_finalize_date=now.date().isoformat())
    service.state_store.path.write_text(json.dumps(state.to_dict()), encoding='utf-8')
    monkeypatch.setattr(service, '_collect', lambda *args, **kwargs: pytest.fail('continuation cannot collect'))
    monkeypatch.setattr(service, '_promote_eligible', lambda ids: (0, (), ()))
    monkeypatch.setattr(request_scope, 'foreground_model_active', lambda backend: True)
    assert service.run_if_due(now=now).status == 'busy'
    assert service.state_store.load().candidate_scan_cursor == {}
    monkeypatch.setattr(request_scope, 'foreground_model_active', lambda backend: False)
    assert service.run_if_due(now=now).status == 'succeeded'
    state = service.state_store.load()
    assert state.candidate_scan_cursor['active'] and not state.pending_reasons
    assert service.backend.calls == 0
    failed = replace(state, last_failure_code='CURATOR_CANDIDATE_READ_FAILED', last_failure_at=(now + timedelta(seconds=30)).isoformat())
    assert candidate_scan_deadline(failed, 60) == (now + timedelta(seconds=90)).timestamp()


def test_stale_lease_cannot_advance_a_slow_read_window(tmp_path, monkeypatch):
    import threading

    from agent_py_agent.agent.memory_store.curator_state import CuratorLeaseLostError
    service = seeded(tmp_path, 85)
    request = CuratorLeaseRequest('interval', service.config.revision(), 30, purpose='candidate_scan')
    acquired = service.committer.recover_and_acquire(request)
    old = acquired[1]['lease_id']
    entered, resume = threading.Event(), threading.Event()
    original = service.candidate_service.readonly_window
    errors = []
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert resume.wait(5)
        return result
    monkeypatch.setattr(service.candidate_service, 'readonly_window', slow)
    def worker():
        try:
            service._pending_promotable_candidate_ids(lease_id=old)
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        release_curator_lease(service.state_store, old)
        fresh = service.committer.recover_and_acquire(request)[1]['lease_id']
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive() and len(errors) == 1 and isinstance(errors[0], CuratorLeaseLostError)
    assert service.state_store.load().active_lease['lease_id'] == fresh
    assert service.state_store.load().candidate_scan_cursor == {}
    release_curator_lease(service.state_store, fresh)


@pytest.mark.parametrize('prior_pages', [0, 2])
def test_promotion_failure_does_not_publish_first_or_final_page_cursor(tmp_path, monkeypatch, prior_pages):
    service = seeded(tmp_path, 85)
    now = datetime.now(timezone.utc)
    for _ in range(prior_pages):
        select_under_lease(service, now)
    before = service.state_store.load().candidate_scan_cursor
    attempted = []
    def interrupt(identifiers):
        attempted.append(tuple(identifiers))
        assert service.state_store.load().candidate_scan_cursor == before
        raise RuntimeError('exit before promotion returns')
    monkeypatch.setattr(service, '_promote_eligible', interrupt)
    assert service._resume_candidate_promotions(now).status == 'failed'
    assert service.state_store.load().candidate_scan_cursor == before
    # 冷实例沿同一原候选事实重试；失败没有提前签轮转完成，也不回放新的提取/日报。
    restarted = _service(tmp_path, _StaticStructuredBackend({}), ConversationStore(tmp_path / 'c'))
    restarted.promotion_callback = lambda candidate_id: None
    def finish(identifiers):
        assert tuple(identifiers) == attempted[0]
        assert restarted.state_store.load().candidate_scan_cursor == before
        return 0, (), ()
    monkeypatch.setattr(restarted, '_promote_eligible', finish)
    assert restarted._resume_candidate_promotions(now + timedelta(seconds=61)).status == 'succeeded'
    assert restarted.state_store.load().candidate_scan_cursor != before
    if prior_pages:
        assert restarted.state_store.load().candidate_scan_cursor['active'] is False
    assert restarted.backend.calls == 0


# === Source: agent_py_agent/tests/test_r9_feishu_user_event_sources.py ===

def user_schedule():
    value = schedule()
    value['source'].update(app_id='receiver', chat_id='group', sender_type='user', sender_id_type='open_id',
        sender_ids=['member'], proofs=[{'sender_id': 'member', 'source_message_id': 'source-message'}])
    return normalize_event_schedule(value)


def observation():
    return {**event(app_id='receiver', chat_id='group', sender_type='user', sender_id_type='open_id',
        sender_id='member', message_id='source-message', created_at=time.time()),
        'message_type': 'text', 'parent_id': '', 'root_id': '', 'thread_id': ''}


def user_read(token, path, **kwargs):
    result = provider_read(token, path, **kwargs)
    if '/messages/' in path:
        result['items'][0]['sender'] = {'sender_type': 'user', 'id_type': 'open_id', 'id': 'member', 'tenant_key': 'chat-tenant'}
    return result


def repo_with_job(tmp_path):
    repo = SchedulerRepository(tmp_path / 'scheduler', owner_provider='feishu', owner_kind='group', owner_id='feishu/groups/group')
    job = repo.create_job(SchedulerJobCreateRequest(name='指定成员', prompt='按原条件研判', thread_id='thread-source',
        schedule=user_schedule(), creator={'channel': 'feishu', 'actor_id': 'owner'}, source_request_id='synthetic',
        delivery={'mode': 'origin', 'reply_policy': 'explicit'}))[0]
    return repo, job


def test_exact_user_schedule_reuses_original_replay_and_cancellation(tmp_path):
    repo, job = repo_with_job(tmp_path)
    incoming = observation()
    assert repo.accept_source_event(incoming)['reason_code'] == 'EVENT_SOURCE_REGISTERED'
    reopened = SchedulerRepository(repo.root, owner_provider='feishu', owner_kind='group', owner_id='feishu/groups/group')
    assert reopened.accept_source_event(incoming)['reason_code'] == 'EVENT_SOURCE_DUPLICATE'
    run = reopened.reserve_event_runs()[0]
    assert run['event_batch'] == [incoming]
    assert run['creator'] == job['creator']


@pytest.mark.parametrize('patch', [dict(sender_id='other'), dict(chat_id='other'), dict(app_id='other'),
    dict(tenant_key='other'), dict(sender_id_type='union_id'), dict(sender_type='bot')])
def test_foreign_user_event_never_enters_pending(tmp_path, patch):
    repo, job = repo_with_job(tmp_path)
    assert repo.accept_source_event({**observation(), **patch})['status'] == 'ignored'
    assert repo.get_job(job['job_id'])['event_source_state']['']['pending'] == []


def test_user_official_get_and_complete_membership_are_required(monkeypatch):
    monkeypatch.setattr(facts, '_read', user_read)
    value = facts.canonical_user_observation(host(), observation())
    assert value['sender_id'] == 'member' and value['tenant_key'] == 'chat-tenant'
    with pytest.raises(facts.FeishuProviderFactError):
        facts.canonical_user_observation(host(), {**observation(), 'sender_id': 'nickname'})
    def incomplete(token, path, **kwargs):
        value = user_read(token, path, **kwargs)
        if '/members?' in path:
            return {'items': [], 'has_more': True, 'page_token': 'repeated'}
        return value
    monkeypatch.setattr(facts, '_read', incomplete)
    with pytest.raises(facts.FeishuProviderFactError, match='FEISHU_GROUP_MEMBERS_INCOMPLETE'):
        facts.canonical_user_observation(host(), observation())


def test_unmentioned_user_uses_observation_and_ignored_never_becomes_conversation():
    current = adapter()
    human = Mock()
    current.on_message(human)
    current.on_observation(lambda _event: {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'})
    assert current._handle_feishu_event(payload('user')).status == 'ignored'
    current.on_observation(lambda _event: True)
    assert current._handle_feishu_event(payload('user')).status == 'accepted'
    human.assert_not_called()


def test_unsubscribed_human_never_enters_durable_ingress(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.manager import ChannelManager
    manager = ChannelManager(delivery_state_dir=tmp_path / 'transport')
    manager._inbound_media_agent = SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path / 'home'))
    current = adapter()
    manager.register_adapter(current)
    monkeypatch.setattr(transport, 'submit_channel_observation', lambda *_a, **_kw:
        {'status': 'ignored', 'reason_code': 'EVENT_SOURCE_NOT_SUBSCRIBED'})
    assert current._handle_feishu_event(payload('user')).status == 'ignored'
    assert manager._delivery_worker.store.pending() == []


def test_real_observation_http_probe_register_replay_and_lease_release(tmp_path, monkeypatch):
    from agent_py_agent.agent.auth.manager import AuthManager
    from agent_py_agent.agent.auth.middleware import AuthMiddleware
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    from agent_py_agent.tests.support.ci_authenticated_gateway import authenticated_headers

    base = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path)
    paths = gateway_paths(base)
    base.scheduler_repository.due_index.mark_source_discovery_complete()
    owner = resolve_owner_home(base.home_paths.root, OwnerIdentity.provider_group('feishu', 'group'))
    repo, job = repo_with_job(tmp_path)
    owner.scheduler_dir.mkdir(parents=True, exist_ok=True)
    (owner.scheduler_dir / 'store.json').write_bytes(repo.store_path.read_bytes())
    scoped = host()
    scoped.scheduler_repository = repo
    leases = []
    @contextmanager
    def lease():
        leases.append('open')
        try:
            yield scoped
        finally:
            leases.append('close')
    def resolve(_base, _owner, *, lease_stack):
        assert _owner == OwnerIdentity.provider_group('feishu', 'group')
        return lease_stack.enter_context(lease())
    monkeypatch.setattr(endpoint, '_resolve_request_agent_for_owner', resolve)
    base.delivery_service = scoped.delivery_service
    monkeypatch.setattr(facts, '_read', user_read)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=base,
        auth_middleware=AuthMiddleware(AuthManager())))
    try:
        server.start()
        manager = SimpleNamespace(gateway_port=server.server.server_address[1],
            _reply_poll_client=SimpleNamespace(credentials=SimpleNamespace(headers=lambda: authenticated_headers(base.home_paths.root))))
        body = observation()
        assert transport.submit_channel_observation(manager, body, probe_only=True)['reason_code'] == 'EVENT_SOURCE_INTEREST_ONLY'
        assert repo.get_job(job['job_id'])['event_source_state']['']['pending'] == []
        assert transport.submit_channel_observation(manager, body)['reason_code'] == 'EVENT_SOURCE_REGISTERED'
        assert transport.submit_channel_observation(manager, body)['reason_code'] == 'EVENT_SOURCE_DUPLICATE'
        monkeypatch.setattr(facts, '_read', lambda *_a, **_kw: (_ for _ in ()).throw(OSError('synthetic')))
        with pytest.raises(OSError):
            transport.submit_channel_observation(manager, body)
        assert leases == ['open', 'close'] * 4
    finally:
        server.stop()


def test_schedule_tool_creates_verified_user_source_under_original_group_grant(tmp_path, monkeypatch):
    from agent_py_agent.agent.scheduler import event_source_tool as sources
    from agent_py_agent.agent.scheduler.tool import ScheduleTool
    from agent_py_agent.tests.test_group_authorization import _allow, _case

    agent, _thread, actor, raw, _calls = _case(tmp_path)
    _allow(agent, raw, actor, subject=actor.actor_id)
    monkeypatch.setattr(sources, 'group_authority_snapshot', lambda *_args: raw)
    monkeypatch.setattr(facts, '_provider', lambda _agent: (SimpleNamespace(app_id=raw['app_id']), 'synthetic-only'))
    def read(_token, path, **_kwargs):
        assert '/messages/om_exact' in path
        return {'items': [{'message_id': 'om_exact', 'chat_id': raw['chat_id'], 'sender': {
            'sender_type': 'user', 'id_type': 'open_id', 'id': actor.actor_id, 'tenant_key': raw['tenant_key']}}]}
    monkeypatch.setattr(facts, '_read', read)
    outcome = ScheduleTool(agent).execute({'action': 'create', 'name': '指定成员', 'prompt': '合成条件',
        'schedule_kind': 'event_source', 'event_source': {'source_message_ids': ['om_exact']}})
    assert outcome.ok, outcome.output
    job = agent.scheduler_repository.list_jobs()[0][0]
    assert job['schedule']['source']['sender_ids'] == [actor.actor_id]
    assert job['schedule']['source']['sender_type'] == 'user'
    assert job['creator'] == {'channel': 'feishu', 'actor_id': actor.actor_id}


def test_user_capability_default_and_strict_false_are_live_deployment_values(tmp_path):
    from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
    from agent_py_agent.agent.settings.live_switches import DEPLOY_LIVE_SWITCH_KEYS

    assert CapabilityConfig().feishu_user_subscriptions_enabled is True
    assert 'feishu_user_subscriptions_enabled' in DEPLOY_LIVE_SWITCH_KEYS
    path = tmp_path / 'capability.yaml'
    path.write_text('feishu_user_subscriptions_enabled: false\n')
    assert load_capability_config(path).feishu_user_subscriptions_enabled is False
    path.write_text('feishu_user_subscriptions_enabled: maybe\n')
    with pytest.raises(ValueError):
        load_capability_config(path)


# === Source: agent_py_agent/tests/test_rework_promotion_item_isolation.py ===

_OPTIONS = {"reviewer": "memory-promotion-service", "automatic": True}
_FORGOTTEN = "用户已要求遗忘的事实 X"
_KEPT = ("独立保留的事实 A", "独立保留的事实 B")


# LLM: 先经正式服务晋升 X，再按精确版本以用户遗忘删除，留下永久正文屏障；随后登记 A、X、B 三条自动合格候选。
# 函数用途: 构造中间一条撞上遗忘屏障、两侧都可正常提交的候选批次。
def _batch_around_forgotten(service, conversations, thread):
    seed = _explicit(service, conversations, thread, content=_FORGOTTEN, subject_key="isolation.seed")
    assert service.promote_batch([seed], **_OPTIONS)[0].promoted
    [record] = service.long_term.all()
    service.long_term.apply_batch([{"action": "remove", "entry_id": record.entry_id, "source": "user-forget",
                                    "expected_version": record.version}])
    assert service.long_term.all() == []
    contents = (_KEPT[0], _FORGOTTEN, _KEPT[1])
    return [_explicit(service, conversations, thread, content=content, subject_key=f"isolation.{index}")
            for index, content in enumerate(contents)]


# LLM: 只经隔离夹具登记互不冲突的用户明确事实，不改证据或自动资格。
# 函数用途: 为注入失败的用例准备三条可自动晋升的候选。
def _independent(service, conversations, thread):
    return [_explicit(service, conversations, thread, content=f"互不相关的事实 {index}",
                      subject_key=f"isolation.independent.{index}") for index in range(3)]


# LLM: 被遗忘正文只让本条带结构化屏障码失败并保留提交义务；两侧候选照常正式提交并收口终态。
# 函数用途: 证明遗忘屏障是逐条失败，不再中止整批。
def test_tombstoned_candidate_in_the_middle_does_not_block_its_batch(tmp_path):
    service, conversations, thread = _runtime(tmp_path)
    rows = _batch_around_forgotten(service, conversations, thread)
    results = service.promote_batch(rows, **_OPTIONS)
    assert [(result.promoted, result.reason_code) for result in results] == [
        (True, "PROMOTED"), (False, "MEMORY_TOMBSTONED_CONTENT"), (True, "PROMOTED")]
    first, forgotten, last = (service.candidates.get(row.candidate_id) for row in rows)
    assert [(row.status, row.commit_obligation) for row in (first, last)] == [("promoted", False)] * 2
    assert (forgotten.status, forgotten.commit_obligation) == ("approved", True)
    assert sorted(record.content for record in service.long_term.all()) == sorted(_KEPT)


# LLM: 组合根注入的批量回调与真实向量库；第二周期只剩 X 的逐条回执，不再出现整块失败警告。
# 函数用途: 证明 Curator 每周期都能提交其余候选，失败候选按结构化回执重试。
def test_curator_cycles_commit_around_a_tombstoned_candidate(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    monkeypatch.setattr(agent.memory, "_embedder", LocalHashingEmbedder(dim=8))
    rows = _batch_around_forgotten(agent.memory_promotion, agent.conversation_store, _thread(agent))
    curator = _curator(agent, rows)
    first = curator.run(reason="admin")
    assert first.status == "succeeded", first
    assert first.formal_commits == 2
    assert [receipt["candidate_id"] for receipt in first.promotion_results] == [row.candidate_id for row in rows]
    assert "CURATOR_AUTO_PROMOTION_FAILED" not in first.warnings
    formal = agent.memory.all()
    assert sorted(record.content for record in formal) == sorted(_KEPT)
    assert sorted(agent.memory._vector_store().ids()) == sorted(record.entry_id for record in formal)
    second = curator.run(reason="admin")
    assert second.status == "succeeded", second
    assert [(receipt["candidate_id"], receipt["promoted"], receipt["reason_code"])
            for receipt in second.promotion_results] == [(rows[1].candidate_id, False, "MEMORY_TOMBSTONED_CONTENT")]
    assert "CURATOR_AUTO_PROMOTION_FAILED" not in second.warnings


# LLM: 未分类的普通异常只映射稳定码，不把异常正文写进结果；失败项保留义务，其它项照常提交。
# 函数用途: 证明任意确定性逐条失败都不会让整批逃逸。
def test_unexpected_item_error_fails_only_that_item(tmp_path, monkeypatch):
    service, conversations, thread = _runtime(tmp_path)
    rows = _independent(service, conversations, thread)
    original = promotion._commit_admitted

    # LLM: 只让选定候选在正式提交前失败，其它候选原样走生产提交。
    # 函数用途: 为一条候选注入普通 RuntimeError。
    def commit(owner, candidate, review, **options):
        if candidate.candidate_id == rows[1].candidate_id:
            raise RuntimeError("simulated deterministic item failure")
        return original(owner, candidate, review, **options)

    monkeypatch.setattr(promotion, "_commit_admitted", commit)
    results = service.promote_batch(rows, **_OPTIONS)
    assert [(result.promoted, result.reason_code) for result in results] == [
        (True, "PROMOTED"), (False, "PROMOTION_ITEM_FAILED"), (True, "PROMOTED")]
    failed = service.candidates.get(rows[1].candidate_id)
    assert (failed.status, failed.commit_obligation) == ("approved", True)
    assert len(service.long_term.all()) == 2


# LLM: 普通异常兜底排在未知结果分支之后；与屏障同为 ValueError 的未知类也必须原对象中止整批。
# 函数用途: 证明逐条隔离不会吞掉执行结果未知。
@pytest.mark.parametrize("error", [
    RequestExecutionContextUnavailable("synthetic missing original source"),
    OwnerManagementIngressUnavailable("synthetic ingress unavailable"),
], ids=["runtime-error", "value-error"])
def test_uncertain_item_outcome_still_aborts_the_batch(tmp_path, monkeypatch, error):
    assert isinstance(error, promotion._UNCERTAIN_PROMOTION_ERRORS)
    service, conversations, thread = _runtime(tmp_path)
    rows = _independent(service, conversations, thread)
    original = promotion._commit_admitted

    # LLM: 只让选定候选抛出未知类异常，其它候选原样走生产提交。
    # 函数用途: 为一条候选注入执行结果未知。
    def commit(owner, candidate, review, **options):
        if candidate.candidate_id == rows[1].candidate_id:
            raise error
        return original(owner, candidate, review, **options)

    monkeypatch.setattr(promotion, "_commit_admitted", commit)
    with pytest.raises(type(error)) as caught:
        service.promote_batch(rows, **_OPTIONS)
    assert caught.value is error


# === Source: agent_py_agent/tests/test_rework_r10_child_lineage_authority.py ===

# 函数用途: 读出运行库为该孩子记录的父 run_id（无父边时为空串），用来与 canonical parent_id 对账。
def _runtime_parent_run_id(agent, task) -> str:
    repo = agent.subagents.runtime_db
    parent_agent_run_id = repo.agent_run_for_run_id(task.id)["parent_agent_run_id"]
    return repo.get_agent_run(parent_agent_run_id)["run_id"] if parent_agent_run_id else ""


def test_non_business_child_under_other_execution_keeps_runtime_parent_equal_to_canonical_parent(tmp_path, monkeypatch):
    agent, params, _, _, _, _ = media_case(tmp_path, monkeypatch)
    actor = agent._current_run_params.host_channel_actor
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    with active_media_child(agent, child) as active:
        raw = {"goal": "在另一执行轮下创建的普通孩子"}
        run_params = create_run_params(agent, raw, raw["goal"], ["read_file"])
        # 前提：非业务创建，且 canonical 谱系父不是当前执行轮；当前执行轮不能替谱系父作证。
        assert run_params.business_context is None
        assert run_params.parent_id and run_params.parent_id != active.id
        assert run_params.host_parent_execution is None
        manual = agent.subagents.create_run(params=run_params)
    assert manual.parent_id == run_params.parent_id
    for task in agent.subagents.list_runs():
        assert _runtime_parent_run_id(agent, task) == task.parent_id
    select_child(agent, manual)
    assert media_for_run(agent, actor) == ()


def test_nested_decision_advice_survives_commit(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    parent = agent.subagents.create_run(goal="父任务", allowed_tools=["create_subagents"])
    agent._current_subagent_run_id = parent.id
    agent._current_run_params.run_id = parent.id
    seen = provider(monkeypatch, prepared, {"0": a})
    task = created(agent, CreateSubagentsTool(agent).execute({"items": [{"goal": "检查材料"}]}))[0]
    assert len(seen) == 1 and task.parent_id == parent.id
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.metadata["host_subagent_model_advice.v1"]["profile_id"] == a
    assert _runtime_parent_run_id(agent, task) == parent.id


def test_scheduled_child_params_carry_canonical_parent_execution(prepared):
    agent, _, _ = prepared
    manager = agent.subagents
    parent = manager.create_run(goal="父任务", allowed_tools=["create_subagents"])
    spec = HierarchyChildSpec(goal="检查材料")
    params = scheduled_child_create_params(manager, parent, spec, sibling_index=1)
    assert params.host_parent_execution.run_id == parent.id
    assert params.host_parent_execution.agent_run_id == manager.runtime_db.agent_run_for_run_id(parent.id)["agent_run_id"]
    # 准备与提交各构造一次：同一事实必须得到相等参数，refreeze 才不会丢弃建议；读范围预检的参数只差父执行来源。
    assert scheduled_child_create_params(manager, parent, spec, sibling_index=1) == params
    assert replace(params, host_parent_execution=None) == _child_create_params(parent, spec, sibling_index=1)
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        scheduled_child_create_params(manager, parent, spec, sibling_index=1)
    assert len(os.listdir("/dev/fd")) == before


# === Source: agent_py_agent/tests/test_rework_r1_event_maintenance.py ===

_STEP_BUDGET = 4096
_TAIL_KEPT = 300
_TAIL_COUNT = 400
_TAIL_TARGETS = {f"seed-{index:06d}" for index in range(_TAIL_KEPT, _TAIL_COUNT)}


def _line(event_id: str, fill: str = "p", width: int = 100) -> bytes:
    return (json.dumps({"event_id": event_id, "payload": {"padding": fill * width}}) + "\n").encode()


# 函数用途: 直接写入SQLite行和同序JSONL，避免逐条fsync拖慢大样本准备。
def _seed(store: LocalStore, rows: list[tuple[str, float]], width: int) -> list[bytes]:
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany("INSERT INTO events(event_id,event_type,payload_json,created_at) VALUES(?, 'seed', '{}', ?)",
                         rows)
    lines = [_line(event_id, width=width) for event_id, _created in rows]
    store.events_path.write_bytes(b"".join(lines))
    return lines


# 函数用途: 最旧的100条放在文件尾部，使已扫描前缀全是保留行，前缀被改写时输出可被区分。
def _seed_tail_targets(store: LocalStore) -> list[bytes]:
    rows = [(f"seed-{index:06d}", 1000.0 + index if index < _TAIL_KEPT else 1.0 + index - _TAIL_KEPT)
            for index in range(_TAIL_COUNT)]
    return _seed(store, rows, 100)


def _step(store: LocalStore) -> dict:
    return LocalStore(store.db_path, enable_fts=False).prune_events_step(
        before=2_000_000_000, keep_last=_TAIL_KEPT, max_delete=_TAIL_COUNT - _TAIL_KEPT, max_bytes=_STEP_BUDGET)


def _jsonl_ids(store: LocalStore) -> list[str]:
    return [json.loads(line)["event_id"] for line in store.events_path.read_bytes().splitlines()]


def _db_ids(store: LocalStore) -> list[str]:
    with sqlite3.connect(store.db_path) as conn:
        return [row[0] for row in conn.execute("SELECT event_id FROM events ORDER BY seq")]


def _metadata(store: LocalStore, key: str):
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _step_temps(store: LocalStore) -> list:
    return sorted(store.events_path.parent.glob(".events-prune-*.step"))


def _assert_settled(store: LocalStore) -> None:
    assert _metadata(store, "events_prune_pending_ids") is None
    assert _metadata(store, "events_prune_cursor") is None
    assert _step_temps(store) == []


# 函数用途: 经原record_event追加一条真实事件；沿用同库FTS设置，避免切换设置触发额外重建事件。
def _append(store: LocalStore, appended: list[str], tag: str) -> None:
    writer = LocalStore(store.db_path, enable_fts=store.enable_fts)
    appended.append(writer.record_event(tag, payload={"tag": tag}).event_id)


@pytest.mark.parametrize("coordinator", [False, True], ids=["local", "coordinator"])
def test_owner_event_maintenance_completes_with_one_append_per_tick(tmp_path, coordinator):
    # Cat3-R4：活动文件不逐行扫描，SQL 有界推进后原字节留待轮转。
    from agent_py_agent.tests.test_local_store_coordinator_io import Door

    directory = tmp_path / "local_store"
    store = LocalStore(directory / "local.db")
    lines = _seed(store, [(f"seed-{index:06d}", 1.0 + index * 1e-6) for index in range(20_500)], 400)
    size = store.events_path.stat().st_size
    assert size > 4 * 1024 * 1024
    budget = 256 * 1024
    bound = math.ceil(size / budget) + 4
    now = time.time() + 60
    appended, reports, offsets = [], [], []
    for tick in range(bound):
        options = {"commit_transition": Door()} if coordinator else {}
        report = _maintain_event_store(directory, now=now, db_path=store.db_path, **options)
        assert report["executed"] is True, report
        assert report["scanned_bytes"] <= budget
        reports.append(report)
        if not report["partial"]:
            break
        offsets.append(report["cursor"]["offset"])
        _append(store, appended, f"tick-{tick}")
    else:
        pytest.fail(f"bounded event maintenance did not finish within {bound} ticks")
    # 每拍之间都有一次真实追加，游标仍严格前进，从不回到0。
    assert offsets == sorted(set(offsets)) and len(offsets) == len(appended)
    assert sum(report["deleted_db"] for report in reports) == 500
    expected = [f"seed-{index:06d}" for index in range(500, 20_500)] + appended
    assert _db_ids(store) == expected
    assert _jsonl_ids(store) == [f"seed-{index:06d}" for index in range(20_500)] + appended
    assert store.events_path.read_bytes().startswith(b"".join(lines))
    _assert_settled(store)


@pytest.mark.parametrize("change", ["replace", "truncate", "regrow", "rewrite_prefix"])
def test_source_change_restarts_scan_without_reusing_stale_output(tmp_path, change):
    # Cat3-R4：没有重写游标或旧输出；外部替换/截断的活动流完整保留。
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    lines = _seed_tail_targets(store)
    for _ in range(3):
        assert not _step(store)["partial"]
    assert _metadata(store, "events_prune_cursor") is None
    assert _step_temps(store) == []
    if change == "replace":
        # 第40行位于文件头样本与边界样本之间，只能靠dev/ino身份识别换代。
        current = [*lines[:40], lines[40].replace(b"p" * 100, b"q" * 100), *lines[41:]]
        replacement = store.events_path.with_name("replacement.jsonl")
        replacement.write_bytes(b"".join(current))
        os.replace(replacement, store.events_path)
    elif change == "truncate":
        current = lines[:50]
        with store.events_path.open("r+b") as handle:
            handle.truncate(len(b"".join(current)))
    elif change == "regrow":
        current = [_line(f"regrow-{index:04d}", "r") for index in range(120)]
        with store.events_path.open("r+b") as handle:
            handle.truncate(0)
            handle.write(b"".join(current))
    else:
        current = [lines[0].replace(b"p" * 100, b"q" * 100), *lines[1:]]
        with store.events_path.open("r+b") as handle:
            handle.write(current[0])
    first = _step(store)
    assert not first["partial"] and first["cursor"] == {}
    assert _step_temps(store) == []
    for _ in range(64):
        if not _step(store)["partial"]:
            break
    else:
        pytest.fail("event maintenance did not finish after the source change")
    assert store.events_path.read_bytes() == b"".join(current)
    assert _db_ids(store) == [f"seed-{index:06d}" for index in range(100, _TAIL_COUNT)]
    _assert_settled(store)


@pytest.mark.parametrize("fault", ["publish", "final_save", "partial_save"])
def test_simulated_io_error_during_step_publication_recovers(tmp_path, monkeypatch, fault):
    # Cat3-R4：逐行发表及部分游标已退役，旧错误叶不得再被触发。
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    lines = _seed_tail_targets(store)
    failures = []
    def failing(*args, **kwargs):
        failures.append(fault)
        raise OSError("retired event rewrite")
    if fault == "publish":
        monkeypatch.setattr(
            "agent_py_agent.agent.common.json_io.write_private_text_file_atomic_unlocked",
            failing,
        )
    else:
        monkeypatch.setattr(maintenance, "_event_step_save", failing)
    report = _step(store)
    assert report["deleted_db"] == 100 and report["deleted_jsonl"] == 0
    assert not report["partial"] and report["scanned_bytes"] == 0
    assert failures == []
    assert _db_ids(store) == [f"seed-{index:06d}" for index in range(100, _TAIL_COUNT)]
    assert store.events_path.read_bytes() == b"".join(lines)
    _assert_settled(store)


def test_append_during_unlocked_scan_is_not_blocked_and_kept_once(tmp_path, monkeypatch):
    # Cat3-R4：追加不等待活动流重写；只在原维护旁锁前交错，活动流全量保留。
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    _seed_tail_targets(store)
    original_lock = maintenance.locked_json_path
    raced: list[str] = []
    reports: list[dict] = []
    raced_tick: list[int] = []

    # 函数用途: 扫描已到达锁外EOF、尚未取JSONL锁时，从另一线程写入一条真实事件。
    def locked(path, **kwargs):
        if path == store.events_path.with_name(store.events_path.name + ".prune") and not raced:
            worker = threading.Thread(target=_append, args=(store, raced, "racing"))
            worker.start()
            worker.join(10)
            assert raced, "append was blocked while the scan ran outside the JSONL lock"
            raced_tick.append(len(reports))
        return original_lock(path, **kwargs)

    monkeypatch.setattr(maintenance, "locked_json_path", locked)
    for _ in range(64):
        reports.append(_step(store))
        assert reports[-1]["scanned_bytes"] <= _STEP_BUDGET
        if not reports[-1]["partial"]:
            break
    else:
        pytest.fail("event maintenance did not finish")
    # 新尾在同一拍剩余预算内于锁内读入，并随该拍发表完整保留一次。
    final = reports[-1]
    assert raced_tick == [len(reports) - 1]
    assert final["cursor"] == {}
    expected = [f"seed-{index:06d}" for index in range(100, _TAIL_COUNT)] + raced
    assert _jsonl_ids(store) == [f"seed-{index:06d}" for index in range(_TAIL_COUNT)] + raced
    assert _db_ids(store) == expected
    _assert_settled(store)
