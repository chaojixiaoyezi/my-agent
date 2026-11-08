# LLM: 使用真实事实写者和临时Owner目录；维护只经既有tool_outputs通道和协调器写门。
# 模块用途: 验证运行事实到期回收的保护条件、分片预算及静息回执兼容。
import gc
import json
import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.common.json_io import locked_json_path, write_private_text_file_atomic
from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_archive import tool_output_retention as retention
from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    ApprovedRuntimeFactSourceRequest,
    RuntimeFactSourceRequest,
    RuntimeFactTerminalRequest,
    update_runtime_fact_terminal,
    write_approved_runtime_fact_source,
    write_runtime_fact_source,
)
from agent_py_agent.agent.memory_store import reference_pins, retention_scan
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.run_intent import _MAX_FACT_FILES
from agent_py_agent.agent.run_intent_index import _locator_path, read_locator
from agent_py_agent.agent.user_space import storage_lanes
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.storage_lanes import (
    STORAGE_LANE_SLICE_ENTRIES,
    StorageLaneBudget,
    StorageLaneRequest,
    owner_conversation_roots,
    resolve_storage_lane,
    validate_lane_receipt,
)


@pytest.fixture
def owner(tmp_path):
    home = ensure_my_agent_home(tmp_path / 'home')
    return home, datetime(2026, 11, 7, tzinfo=timezone.utc).timestamp()


def request(owner, *, items=STORAGE_LANE_SLICE_ENTRIES, hold=False, commit=None, cursor=None):
    home, now = owner
    return StorageLaneRequest('tool_outputs', home, now,
        {'next_pass_at': now + 3600} if cursor is None else cursor,
        StorageLaneBudget(items, None, None), hold, commit is not None, None, commit)


def fact(owner, name, *, phase='final', age=31, approved=False, root=None, run_id='', task_id=''):
    home, now = owner
    root = Path(root or home.owner_home_dir)
    directory = Path(write_runtime_fact_source(RuntimeFactSourceRequest(
        root=root, request_id=name, status='DONE' if phase == 'final' else 'running', run_id=run_id, task_id=task_id)))
    if phase == 'failed':
        update_runtime_fact_terminal(RuntimeFactTerminalRequest(root=root, request_id=name, status='failed'))
    if approved:
        write_approved_runtime_fact_source(ApprovedRuntimeFactSourceRequest(
            root=root, fact_id=name, goal='已批准事实', next_actions=[], acceptance=[], constraints=[], latest_tests=[]))
    stamp = now - age * 86400
    os.utime(directory / 'task.json', (stamp, stamp))
    return directory


def fresh_locator(owner):
    for index in range(_MAX_FACT_FILES):
        fact(owner, f'recent-{index}', phase='running', age=0)


@pytest.mark.parametrize('phase', ['final', 'failed'])
@pytest.mark.parametrize('resting', [True, False])
def test_old_terminal_facts_are_deleted_with_exact_metrics(owner, phase, resting):
    directory = fact(owner, 'expired', phase=phase)
    fresh_locator(owner)
    size = sum(path.stat().st_size for path in directory.iterdir())
    result = retention.maintain_owner_tool_outputs_lane(request(owner, cursor=None if resting else {}))
    validate_lane_receipt(result)
    assert not directory.exists()
    assert result['items'] == result['deleted_items'] == 1
    assert result['freed_bytes'] == size > 0
    assert result['bytes'] == 0


@pytest.mark.parametrize('reason', ['running', 'approved', 'young', 'protected', 'symlink', 'nested', 'many_files'])
def test_protected_and_unsafe_facts_stay_intact(owner, tmp_path, reason):
    directory = fact(owner, 'keep', phase='running' if reason == 'running' else 'final',
                     age=1 if reason == 'young' else 31, approved=reason == 'approved')
    if reason != 'protected':
        fresh_locator(owner)
    if reason == 'symlink':
        target = tmp_path / 'outside.txt'
        target.write_text('外部正文')
        (directory / 'attachment').symlink_to(target)
    elif reason == 'nested':
        (directory / 'nested').mkdir()
    elif reason == 'many_files':
        for index in range(9):
            (directory / f'extra-{index}').write_text('保留')
    rows, errors = read_locator(directory.parent, limit=_MAX_FACT_FILES)
    assert not errors
    assert (directory.name in {row['directory'] for row in rows}) == (reason == 'protected')
    before = {path.name for path in directory.iterdir()}
    result = retention.maintain_owner_tool_outputs_lane(request(owner))
    assert directory.is_dir() and {path.name for path in directory.iterdir()} == before
    assert result.get('deleted_items', 0) == 0 and not result.get('errors')
    if reason == 'symlink':
        assert target.read_text() == '外部正文'


def test_legal_hold_and_task_local_facts_are_untouched(owner):
    directory = fact(owner, 'held')
    task_fact = fact(owner, 'task-local', root=owner[0].owner_home_dir / 'tasks' / 'task-one')
    fresh_locator(owner)
    commits = []
    result = retention.maintain_owner_tool_outputs_lane(request(owner, hold=True, commit=commits.append))
    assert result['deferred_reason'] == 'legal_hold'
    assert directory.exists() and task_fact.exists() and commits == []
    result = retention.maintain_owner_tool_outputs_lane(request(owner))
    assert not directory.exists() and task_fact.exists()
    assert result['deleted_items'] == 1


def test_owner_retention_override_is_applied_even_while_tool_outputs_rest(owner):
    directory = fact(owner, 'two-hundred-days', age=200)
    fresh_locator(owner)
    policy = owner[0].owner_retention_json
    # 引用复核需要现行完整策略，不能用缺少 schema 的单字段夹具关闭删除。
    payload = {**MemoryRetentionPolicy().to_dict(), 'tool_output_days': 365}
    write_private_text_file_atomic(policy, json.dumps(payload))
    # 事实遍历计入检查条数并在走完后记录休息点（scale-s8）；保留期覆盖仍使200天事实保留。
    kept = retention.maintain_owner_tool_outputs_lane(request(owner))
    assert kept['complete'] is True and kept['items'] == 1 and 'deleted_items' not in kept
    assert kept['cursor']['next_pass_at'] == owner[1] + 3600 and kept['cursor']['facts_after'] == ''
    assert directory.exists()
    write_private_text_file_atomic(policy, json.dumps({**payload, 'tool_output_days': 30}))
    assert retention.maintain_owner_tool_outputs_lane(request(owner))['deleted_items'] == 1
    assert not directory.exists()


@pytest.mark.parametrize('items', [7, STORAGE_LANE_SLICE_ENTRIES])
def test_bounded_calls_drain_three_hundred_facts_only_inside_run_write(owner, monkeypatch, items):
    directories = [fact(owner, f'old-{index}') for index in range(300)]
    fresh_locator(owner)
    # 这里只验证条数上限，独立的过期预算用例验证真实时间门。
    monkeypatch.setattr(StorageLaneBudget, 'remaining_ms', lambda self: 1000.0)
    commits, receipts = [], []
    inside = False
    retire = retention._retire_fact_dir

    def checked_retire(*args):
        assert inside, '删除必须位于协调器写门内部'
        return retire(*args)

    def commit(operation):
        nonlocal inside
        inside = True
        try:
            result = operation()
            commits.append(result)
            return result
        finally:
            inside = False

    monkeypatch.setattr(retention, '_retire_fact_dir', checked_retire)
    for _ in range((300 + items - 1) // items):
        result = retention.maintain_owner_tool_outputs_lane(request(owner, items=items, commit=commit))
        validate_lane_receipt(result)
        assert 0 < result['deleted_items'] <= min(items, STORAGE_LANE_SLICE_ENTRIES)
        receipts.append(result)
    assert sum(row['deleted_items'] for row in receipts) == len(commits) == 300
    assert all(type(size) is int and size > 0 for size in commits)
    assert all(not directory.exists() for directory in directories)


def test_expired_budget_does_not_enter_write_gate(owner):
    directory = fact(owner, 'expired-budget')
    fresh_locator(owner)
    commits = []
    req = replace(request(owner, commit=commits.append),
                  budget=StorageLaneBudget(128, None, time.monotonic() - 1))
    retention.maintain_owner_tool_outputs_lane(req)
    assert directory.exists() and commits == []


def test_absent_facts_preserve_exact_four_key_resting_receipt(owner):
    assert not (owner[0].owner_home_dir / 'memory_archive' / 'runtime_facts').exists()
    expected = {'cursor': {'next_pass_at': owner[1] + 3600}, 'complete': True, 'items': 0, 'bytes': 0}
    result = retention.maintain_owner_tool_outputs_lane(request(owner))
    assert set(result) == {'cursor', 'complete', 'items', 'bytes'}
    assert json.dumps(result).encode() == json.dumps(expected).encode()


def test_no_dir_fd_host_skips_without_errors(owner, monkeypatch):
    directory = fact(owner, 'portable')
    fresh_locator(owner)
    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)
    result = retention.maintain_owner_tool_outputs_lane(request(owner))
    assert directory.exists()
    assert result == {'cursor': {'next_pass_at': owner[1] + 3600}, 'complete': True, 'items': 0, 'bytes': 0}


@pytest.mark.parametrize('invalid', [False, True])
def test_missing_locator_is_allowed_but_invalid_locator_fails_closed(owner, invalid):
    directory = fact(owner, 'locator')
    locator = _locator_path(directory.parent)
    if invalid:
        write_private_text_file_atomic(locator, '{}')
    else:
        locator.unlink()
    result = retention.maintain_owner_tool_outputs_lane(request(owner))
    assert directory.exists() is invalid
    assert result.get('errors', []) == ([{'code': 'runtime_facts_locator_invalid'}] if invalid else [])
    assert result.get('deleted_items', 0) == (0 if invalid else 1)


def test_busy_task_lock_is_preserved(owner):
    directory = fact(owner, 'busy')
    fresh_locator(owner)
    ready, release = threading.Event(), threading.Event()

    def hold():
        with locked_json_path(directory / 'task.json'):
            ready.set()
            assert release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        result = retention.maintain_owner_tool_outputs_lane(request(owner))
        assert directory.exists() and not result.get('errors')
        assert result.get('deleted_items', 0) == 0
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize('change', ['running', 'approved', 'young'])
def test_age_phase_and_source_are_rechecked_inside_write_gate(owner, change):
    directory = fact(owner, 'changed')
    fresh_locator(owner)
    calls = []

    def commit(operation):
        calls.append(True)
        fact(owner, 'changed', phase='running' if change == 'running' else 'final',
             approved=change == 'approved', age=0 if change == 'young' else 31)
        return operation()

    result = retention.maintain_owner_tool_outputs_lane(request(owner, commit=commit))
    assert calls == [True]
    assert directory.exists() and not result.get('errors')
    assert result.get('deleted_items', 0) == 0


def lane_pass(owner, *, cursor=None, commit=None):
    """从登记入口运行有限轮次，游标和回执经过真实 JSON 往返及合同校验。"""
    lane = resolve_storage_lane('tool_outputs')
    assert lane is retention.maintain_owner_tool_outputs_lane
    receipts = []
    for _ in range(8):
        receipt = validate_lane_receipt(json.loads(json.dumps(lane(request(owner, cursor=cursor, commit=commit)))))
        receipts.append(receipt)
        if receipt['complete']:
            return receipts
        cursor = receipt['cursor']
    pytest.fail('事实退休未在有限切片内完成：' + repr(receipts[-1]))


@contextmanager
def no_fd_growth():
    gc.collect()
    fd_root = '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
    before = len(os.listdir(fd_root))
    try:
        yield
    finally:
        gc.collect()
        assert len(os.listdir(fd_root)) - before == 0


@pytest.mark.parametrize('resting', [False, True])
@pytest.mark.parametrize('reference', ['path', 'member', 'request_id', 'run_id', 'task_id'])
def test_formal_memory_refs_protect_old_facts(owner, reference, resting):
    directory = fact(owner, 'referenced', run_id='retained-run', task_id='retained-task')
    control = fact(owner, 'unreferenced')
    fresh_locator(owner)
    member = directory / 'proof.txt'
    member.write_text('原始证据', encoding='utf-8')
    task = directory / 'task.json'
    value = {'path': str(task), 'member': str(member), 'request_id': directory.name,
             'run_id': 'retained-run', 'task_id': 'retained-task'}[reference]
    JsonlMemory(owner[0].owner_memory_long_term_jsonl).add('user', '保留来源', attributes={'source_refs': [value]})
    errors = []
    pins, complete = retention_scan.current_retention_pins(owner[0], owner_conversation_roots(owner[0]), errors,
        now=datetime.fromtimestamp(owner[1], timezone.utc))
    assert complete and pins.protects(directory if reference == 'member' else task, errors) and not errors
    rows, errors = read_locator(directory.parent, limit=_MAX_FACT_FILES)
    assert not errors and directory.name not in {row['directory'] for row in rows}
    before = {path.name: path.read_bytes() for path in directory.iterdir()}

    with no_fd_growth():
        receipts = lane_pass(owner, cursor=None if resting else {})

    assert directory.exists() and {path.name: path.read_bytes() for path in directory.iterdir()} == before
    assert not control.exists() and sum(row.get('deleted_items', 0) for row in receipts) == 1
    assert not any(row.get('errors') for row in receipts)


@pytest.mark.parametrize('resting', [False, True])
def test_active_compact_checkpoint_refs_protect_old_facts(owner, resting):
    from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy

    directory = fact(owner, 'checkpoint-source', run_id='retained-run')
    control = fact(owner, 'checkpoint-control')
    fresh_locator(owner)
    home = owner[0]
    store = ConversationStore(home.owner_home_dir / 'conversations', compact_root=home.owner_compact_dir)
    thread = store.threads.get_or_create({'canonical_user_id': home.owner_id})
    message = store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '保留运行来源'})
    offset = store.storage.message_path(thread.thread_id).stat().st_size
    agent = SimpleNamespace(home_paths=home)
    policy = RuntimeCompactPolicy(context_window_tokens=1000, trigger_percent=90, trigger_tokens=900,
        recovery_target_percent=60, recovery_target_tokens=600, allow_persistent_apply=True,
        recent_tail_max_turns=4, recent_tail_tokens=100, failure_threshold=3, failure_cooldown_seconds=300.0)
    checkpoint_id = checkpoints.write_compact_checkpoint(agent, checkpoints.CompactCheckpointRequest(
        thread=thread, summary='运行来源仍用于恢复', operation_evidence={}, compact_rows=(message,), retained_tail=(),
        source_end_byte_offset=offset, projected_tokens_before=900, projected_tokens_after=300,
        policy=policy, forced=False, source_tool_refs=({'run_id': 'retained-run', 'attempt_id': 'retained-attempt',
                                                     'turn_id': 'retained-turn', 'call_id': 'retained-call'},)))
    committed = store.threads.update_compact_state(thread.thread_id, commit=ConversationCompactCommit(
        summary='运行来源仍用于恢复', operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=message.message_id, compacted_through_byte_offset=offset,
        source_messages=1, source_tool_pairs=1), expected_generation=0)
    chain = checkpoints.committed_compact_checkpoint_chain(agent, committed)
    assert chain[-1]['checkpoint_id'] == checkpoint_id
    assert chain[-1]['source_tool_refs'][0]['run_id'] == 'retained-run'
    before = (directory / 'task.json').read_bytes()

    with no_fd_growth():
        receipts = lane_pass(owner, cursor=None if resting else {})

    assert directory.exists() and (directory / 'task.json').read_bytes() == before
    assert not control.exists() and sum(row.get('deleted_items', 0) for row in receipts) == 1
    assert not any(row.get('errors') for row in receipts)


@pytest.mark.parametrize('resting', [False, True])
@pytest.mark.parametrize('damage', ['malformed', 'unreadable'])
def test_unreadable_pin_sources_preserve_facts_and_diagnostics(owner, monkeypatch, resting, damage):
    directories = [fact(owner, f'unknown-pins-{index}') for index in range(2)]
    fresh_locator(owner)
    memory = JsonlMemory(owner[0].owner_memory_long_term_jsonl)
    memory.add('user', '正式来源', attributes={'source_refs': [str(directories[0] / 'task.json')]})
    if damage == 'malformed':
        memory.path.write_text('{', encoding='utf-8')
    else:
        original = retention_scan._pin_rows

        def unreadable(path, *args, **kwargs):
            if path == memory.path:
                raise PermissionError('private pin source detail')
            yield from original(path, *args, **kwargs)

        monkeypatch.setattr(retention_scan, '_pin_rows', unreadable)
    before = [(directory / 'task.json').read_bytes() for directory in directories]

    with no_fd_growth():
        receipts = lane_pass(owner, cursor=None if resting else {})

    assert all(directory.exists() for directory in directories)
    assert [(directory / 'task.json').read_bytes() for directory in directories] == before
    assert sum(row.get('deleted_items', 0) for row in receipts) == 0
    errors = [error for row in receipts for error in row.get('errors', [])]
    assert any(error['code'] == 'runtime_fact_unreadable' for error in errors)
    assert any(error['code'] == 'MEMORY_RETENTION_PIN_SOURCE_UNREADABLE' for error in errors)
    assert all(len(row.get('errors', [])) <= 16 for row in receipts)
    assert 'private pin source detail' not in json.dumps(receipts)


def test_reference_published_in_write_gate_is_checked_under_fact_locks(owner, monkeypatch):
    directory = fact(owner, 'late-reference')
    fresh_locator(owner)
    memory = JsonlMemory(owner[0].owner_memory_long_term_jsonl)
    original_lock, original_current = retention.locked_json_path, reference_pins.ReferencePinSnapshot.current
    inside, held_locks, observed = [], [], []

    @contextmanager
    def observed_lock(path, **kwargs):
        with original_lock(path, **kwargs):
            held_locks.append(path)
            try:
                yield
            finally:
                held_locks.pop()

    def current(snapshot, errors, *, target=None):
        assert inside and len(held_locks) == 2 and held_locks[-1] == directory / 'task.json'
        observed.append(target)
        return original_current(snapshot, errors, target=target)

    def commit(operation):
        memory.add('user', '写门内新增来源', attributes={'source_refs': [str(directory / 'task.json')]})
        inside.append(True)
        try:
            return operation()
        finally:
            inside.pop()

    monkeypatch.setattr(retention, 'locked_json_path', observed_lock)
    monkeypatch.setattr(reference_pins.ReferencePinSnapshot, 'current', current)
    with no_fd_growth():
        receipts = lane_pass(owner, commit=commit)
    assert directory.exists() and observed == [directory / 'task.json']
    assert not any(row.get('errors') for row in receipts)


def test_stable_fact_candidates_reuse_one_full_pin_build(owner, monkeypatch):
    directories = [fact(owner, f'stable-pins-{index}') for index in range(2)]
    fresh_locator(owner)
    JsonlMemory(owner[0].owner_memory_long_term_jsonl).add('user', '共享引用投影',
        attributes={'source_refs': [str(directory / 'task.json') for directory in directories]})
    original, builds = retention_scan.current_retention_pins, []

    def counted(*args, **kwargs):
        builds.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(retention_scan, 'current_retention_pins', counted)
    # 本例只核对一次切片的解析次数；时间预算仍用原计算，冻结时钟避免调度延迟拆成两片。
    monkeypatch.setattr(storage_lanes, 'time', SimpleNamespace(monotonic=lambda: 100.0))
    with no_fd_growth():
        receipts = lane_pass(owner)
    assert len(receipts) == 1 and receipts[0]['items'] == 2 and builds == [True]
    assert all(directory.exists() for directory in directories)
    assert not receipts[0].get('errors')


def _approved_writer_at_lock(root, ready, proceed, attempted, blocked, completed, results):
    """子进程使用真实写者；只在原锁获取口报告已取得或正在等待，不替换磁盘写入。"""
    from agent_py_agent.agent.common import json_io

    acquire = json_io._acquire_verified_lock_descriptor
    observed = False

    def observe(path, *, blocking=True, **options):
        nonlocal observed
        if observed:
            return acquire(path, blocking=blocking, **options)
        observed = True
        try:
            descriptor = acquire(path, blocking=False, **options)
        except BlockingIOError:
            blocked.set()
            attempted.set()
            return acquire(path, blocking=blocking, **options)
        attempted.set()
        return descriptor

    json_io._acquire_verified_lock_descriptor = observe
    ready.set()
    try:
        assert proceed.wait(10)
        directory = Path(write_approved_runtime_fact_source(ApprovedRuntimeFactSourceRequest(
            root=Path(root), fact_id='concurrent', goal='批准事实必须保留', next_actions=[],
            acceptance=['保留批准事实'], constraints=[], latest_tests=[])))
        results.put({'path': str(directory), 'source': json.loads((directory / 'task.json').read_text())['source']})
    except BaseException as error:
        results.put({'error': type(error).__name__, 'message': str(error)})
    finally:
        json_io._acquire_verified_lock_descriptor = acquire
        completed.set()


@pytest.mark.parametrize('writer_first', [False, True])
@pytest.mark.parametrize('lock_first', [False, True])
def test_approved_writer_and_retirement_share_cross_process_exclusion(owner, monkeypatch, writer_first, lock_first):
    directory = fact(owner, 'concurrent', age=40)
    fresh_locator(owner)
    rows, errors = read_locator(directory.parent, limit=_MAX_FACT_FILES)
    assert not errors and directory.name not in {row['directory'] for row in rows}
    assert nofollow_fs._supports_dir_fd(), '本回归必须实际进入dir_fd退休路径'
    identity = directory.stat().st_ino
    scan, unlink = os.scandir, os.unlink
    ctx = multiprocessing.get_context('spawn')
    ready, proceed, attempted, blocked, completed = (ctx.Event() for _ in range(5))
    # Event已启动进程级resource tracker；其FD是继承基线，子进程及Queue的FD仍须完整归还。
    gc.collect()
    fd_root = '/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd'
    before = len(os.listdir(fd_root))
    results = ctx.Queue()
    child = ctx.Process(target=_approved_writer_at_lock, args=(
        str(owner[0].owner_home_dir), ready, proceed, attempted, blocked, completed, results))
    child.start()
    observed = []

    @contextmanager
    def ordered_scan(path):
        with scan(path) as entries:
            if isinstance(path, int) and os.fstat(path).st_ino == identity:
                members = sorted(entries, key=lambda entry: (entry.name.endswith('.lock') != lock_first, entry.name))
                yield iter(members)
            else:
                yield entries

    def interleaved_unlink(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == 'task.json.lock' and 'dir_fd' in kwargs and not writer_first:
            observed.append(path)
            proceed.set()
            assert attempted.wait(10)
            if not blocked.is_set():
                assert completed.wait(10)
        return result

    def commit(operation):
        if writer_first:
            proceed.set()
            assert completed.wait(10)
        return operation()

    try:
        assert ready.wait(10)
        # 只替换退休模块的OS适配器，保持全局os.supports_dir_fd中的函数身份真实不变。
        observed_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
        observed_os.scandir, observed_os.unlink = ordered_scan, interleaved_unlink
        with monkeypatch.context() as patch:
            patch.setattr(retention, 'os', observed_os)
            receipt = retention.maintain_owner_tool_outputs_lane(request(owner, commit=commit))
        assert receipt['items'] > 0, '必须经过真实候选复核，不能因能力检测变化而跳过'
        proceed.set()
        assert completed.wait(10)
        child.join(10)
        assert not child.is_alive() and child.exitcode == 0
        result = results.get(timeout=5)
        assert result == {'path': str(directory), 'source': 'approved_runtime_fact_source'}
        assert json.loads((directory / 'task.json').read_text())['source'] == 'approved_runtime_fact_source'
        assert not receipt.get('errors')
        assert receipt.get('deleted_items', 0) == (0 if writer_first else 1)
        if not writer_first:
            assert observed == ['task.json.lock'] and blocked.is_set()
    finally:
        proceed.set()
        child.join(2)
        if child.is_alive():
            child.terminate()
            child.join(5)
        child.close()
        results.close()
        results.join_thread()
        del results, child
        deadline = time.monotonic() + 5
        while True:
            gc.collect()
            fd_delta = len(os.listdir(fd_root)) - before
            if fd_delta == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        assert fd_delta == 0
