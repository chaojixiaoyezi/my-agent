"""W18：完整引用链逐片执行、代次失效和执行前关闭式保护。"""
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.reference_pins import ReferencePinSnapshot
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def close_projections(monkeypatch):
    # 使用确定性的已冷却来源，收敛断言不依赖测试机两秒内的IO吞吐。
    real_ns = time.time_ns
    monkeypatch.setattr(scan.reference_pins, "time", SimpleNamespace(time_ns=lambda: real_ns() + 10_000_000_000))
    yield
    with scan._PIN_BUILDS_LOCK:
        projections = list({id(value): value for value in (
            *scan._CURRENT_PIN_BUILDS.values(), *scan._PIN_BUILDS.values())}.values())
        scan._CURRENT_PIN_BUILDS.clear()
        scan._PIN_BUILDS.clear()
    for projection in projections:
        projection.close()


def runtime(tmp_path, count=12, *, legacy=False):
    home = ensure_my_agent_home(tmp_path / 'home')
    service = CandidateService(home.owner_memory_candidates_jsonl)
    candidates = service.observe_many([
        CandidateObservation(candidate_type='long_term_fact', content=f'测试记录{i}', subject_key=f'item.{i}',
                             scope=MemoryScope('personal', 'personal'), origin='model_inferred',
                             source_artifact_refs=({'path': f'assets/pin-{i}.bin'},))
        for i in range(count)
    ])
    if legacy:
        Path(service.path).write_text(''.join(json.dumps(row.to_record()) + '\n' for row in candidates))
    return home, service


def advance(home, *, entries=8, size=512, errors=None):
    budget = RetentionScanBudget(entries, size, float('inf'))
    result = scan.current_retention_pins(home, (), errors if errors is not None else [], now=NOW, scan_budget=budget)
    assert budget.entries <= entries
    assert budget.size <= size
    return result


def converge(home, **kwargs):
    for tick in range(3000):
        result = advance(home, **kwargs)
        if result[1]:
            return result[0], tick
        assert result[0].complete is False
        assert result[0].complete_for(Path(home.owner_home_dir) / 'unseen', []) is False
    pytest.fail('引用扫描没有收敛')


@pytest.mark.parametrize('legacy', [False, True])
def test_whole_chain_budget_and_multitick_convergence(tmp_path, legacy):
    home, _ = runtime(tmp_path, legacy=legacy)
    pins, ticks = converge(home)
    assert ticks > 1
    assert Path(home.owner_home_dir) / 'assets/pin-11.bin' in pins.paths
    assert len(pins.ids) >= 12


def test_cached_generation_does_not_reparse_candidates_per_action(tmp_path, monkeypatch):
    home, _ = runtime(tmp_path)
    converge(home)
    def unexpected(*args):
        raise AssertionError('同代次逐动作重扫候选')
    monkeypatch.setattr(scan._PinProjection, '_candidate_rows', unexpected)
    for _ in range(3):
        pins, _ticks = converge(home)
        assert Path(home.owner_home_dir) / 'assets/pin-11.bin' in pins.paths


def test_manifest_mutation_invalidates_completed_projection(tmp_path):
    home, service = runtime(tmp_path)
    converge(home)
    service.observe(CandidateObservation(candidate_type='long_term_fact', content='新引用', subject_key='new',
                    scope=MemoryScope('personal', 'personal'), origin='model_inferred',
                    source_artifact_refs=({'path': 'assets/new.bin'},)))
    result = advance(home)
    assert result[1] is False
    pins, _ = converge(home)
    assert Path(home.owner_home_dir) / 'assets/new.bin' in pins.paths


def test_mid_build_mutation_never_publishes_old_generation(tmp_path):
    home, service = runtime(tmp_path, count=20)
    assert not advance(home)[1]
    service.observe(CandidateObservation(candidate_type='long_term_fact', content='变化', subject_key='changed',
                    scope=MemoryScope('personal', 'personal'), origin='model_inferred',
                    source_artifact_refs=({'path': 'assets/changed.bin'},)))
    pins, _ = converge(home)
    assert Path(home.owner_home_dir) / 'assets/changed.bin' in pins.paths


def test_corrupt_candidate_never_means_empty_pins_and_other_owner_continues(tmp_path):
    broken, _ = runtime(tmp_path / 'bad', legacy=True)
    Path(broken.owner_memory_candidates_jsonl).write_text('{broken\n')
    errors = []
    for _ in range(100):
        pins, complete = advance(broken, errors=errors)
        assert not complete and not pins.complete
        if errors:
            break
    assert errors
    healthy, _ = runtime(tmp_path / 'healthy')
    pins, _ = converge(healthy)
    assert pins.complete


def test_snapshot_explicit_resumable_mode_does_not_cache_stale_proof(tmp_path):
    home, _ = runtime(tmp_path)
    config = SimpleNamespace(retention_scan_slice_entries=1, retention_scan_slice_bytes=512,
                             retention_scan_slice_ms=1000)
    snapshot = ReferencePinSnapshot(home, (), NOW, scan.current_retention_pins, config=config, resumable=True)
    with pytest.raises(OSError, match='SCAN_PENDING: not_started'):
        snapshot.current([])
    converge(home)
    Path(home.owner_memory_candidates_jsonl).write_text('{corrupt\n')
    with pytest.raises(OSError, match='SCAN_PENDING: not_started'):
        snapshot.current([])


def test_concurrent_borrow_does_not_wait_or_publish_partial(tmp_path):
    home, _ = runtime(tmp_path)
    advance(home)
    projection = scan._CURRENT_PIN_BUILDS[Path(home.owner_home_dir)]
    acquired, release = Event(), Event()
    def borrower():
        with projection.lock:
            acquired.set()
            assert release.wait(5)
    thread = Thread(target=borrower)
    thread.start()
    assert acquired.wait(5)
    try:
        pins, complete = advance(home)
        assert not complete and not pins.complete
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert converge(home)[0].complete


def test_candidate_action_enumeration_reuses_budgeted_rows_and_digest(tmp_path, monkeypatch):
    home, service = runtime(tmp_path)
    records = service.list()
    raw = ''.join(json.dumps(replace(row, updated_at='2020-01-01T00:00:00+00:00',
                                    last_observed_at='2020-01-01T00:00:00+00:00').to_record()) + '\n'
                  for row in records).encode()
    Path(service.path).write_bytes(raw)
    def no_full_scan(*args, **kwargs):
        raise AssertionError('不能绕过引用流重新全量枚举候选')
    monkeypatch.setattr(service, 'readonly_rows', no_full_scan)
    monkeypatch.setattr(service, 'list', no_full_scan)
    cursor = {}
    for _ in range(3000):
        budget = RetentionScanBudget(8, 512, float('inf'))
        report = scan.build_retention_plan(home=home, candidates=service, now=NOW,
                                          scan_budget=budget, scan_cursor=cursor)
        cursor = report.scan_cursor
        assert budget.entries <= 8 and budget.size <= 512
        if not report.scan_partial:
            break
    else:
        pytest.fail('候选动作扫描没有收敛')
    actions = [action for action in report.actions if action.operation == 'candidate_delete']
    assert {action.record_id for action in actions} == {row.candidate_id for row in records}
    assert {action.authority_fingerprint for action in actions} == {hashlib.sha256(raw).hexdigest()}
    # 规划完成投影直接转给执行缓存，没有第二份候选解析。
    monkeypatch.setattr(scan._PinProjection, '_candidate_rows', no_full_scan)
    assert converge(home)[0].complete


def test_expired_deadline_does_not_advance_reference_stream(tmp_path):
    home, _ = runtime(tmp_path)
    budget = RetentionScanBudget(8, 512, 0)
    pins, complete = scan.current_retention_pins(home, (), [], now=NOW, scan_budget=budget)
    assert not complete and not pins.complete
    assert budget.entries == 0 and budget.size == 0
    assert converge(home)[0].complete


def test_scheduled_cleanup_requires_terminal_proof_again_at_apply(tmp_path, monkeypatch):
    import importlib
    import sys
    from types import ModuleType

    module_name = 'agent_py_agent.agent.conversation.thread_read_index'
    try:
        thread_read_index = importlib.import_module(module_name)
    except ModuleNotFoundError:
        # 分片基线尚未合入W18a；这里只验证调用者双重门，真实canonical读取由W18a及集成验收覆盖。
        thread_read_index = ModuleType(module_name)
        monkeypatch.setitem(sys.modules, module_name, thread_read_index)
    from agent_py_agent.agent.conversation.models import ConversationThread
    from agent_py_agent.agent.memory_store.retention_apply import _trash_conversation
    from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy

    home = ensure_my_agent_home(tmp_path / 'home')
    root = Path(home.owner_home_dir) / 'conversations'
    (root / 'threads').mkdir(parents=True, exist_ok=True)
    (root / 'messages').mkdir(parents=True, exist_ok=True)
    thread = ConversationThread(thread_id='thread-sched-1', canonical_user_id='test', updated_at=1,
                                metadata={'thread_kind': 'scheduled', 'scheduler_run_id': 'sched-1'})
    path = root / 'threads' / f'{thread.thread_id}.json'
    path.write_text(json.dumps(thread.to_dict()))
    for name in ('channel_bindings.json', 'user_latest_threads.json'):
        (root / name).write_text('{}')
    state = {'status': 'unknown'}
    calls = []
    def proof(home, thread, cutoff, **kwargs):
        calls.append((thread.thread_id, cutoff))
        return dict(state)
    monkeypatch.setattr(thread_read_index, 'scheduled_thread_terminal_proof', proof, raising=False)
    kwargs = dict(home=home, policy=MemoryRetentionPolicy(), held_task_ids=frozenset(), roots=(root,), now=NOW, errors=[])
    assert scan._conversation_actions(**kwargs) == []
    state['status'] = 'settled'
    actions = scan._conversation_actions(**kwargs)
    assert len(actions) == 1
    state['status'] = 'unknown'
    result = _trash_conversation(home=home, action=actions[0], now=NOW)
    assert result.status == 'state_changed'
    assert path.exists()
    assert len(calls) == 3
