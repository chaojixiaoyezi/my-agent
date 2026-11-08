# LLM: scale-s8 回归（scale-s2残留）：运行事实遍历按facts_after续扫直到走完、走完前通道保持未完成，
#   只删终态阶段（started/tool_loop/running保留），换出或年轻条目占据目录头部时仍能推进到底。
# 模块用途: 用真实事实写者构造owner布局，验证终态白名单、续点覆盖、预算、写门、legal_hold与无dir_fd平台。

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.memory_archive import tool_output_retention as retention
from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    ApprovedRuntimeFactSourceRequest,
    RuntimeFactSourceRequest,
    RuntimeFactTerminalRequest,
    update_runtime_fact_terminal,
    write_approved_runtime_fact_source,
    write_runtime_fact_source,
)
from agent_py_agent.agent.user_space import storage_lanes
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.storage_lanes import (
    STORAGE_LANE_SLICE_ENTRIES,
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)

DAY = 86400


@pytest.fixture
def home(tmp_path):
    paths = ensure_my_agent_home(tmp_path / 'home')
    paths.owner_home_dir.mkdir(parents=True, exist_ok=True)
    return paths


def facts_dir(home):
    return Path(home.owner_home_dir) / 'memory_archive' / 'runtime_facts'


def fact(home, name, *, phase='final', age_days=0):
    root = Path(home.owner_home_dir)
    if phase == 'approved':
        write_approved_runtime_fact_source(ApprovedRuntimeFactSourceRequest(
            root=root, fact_id=name, goal='g', next_actions=[], acceptance=[], constraints=[], latest_tests=[]))
    elif phase == 'failed':
        write_runtime_fact_source(RuntimeFactSourceRequest(root=root, request_id=name, user_prompt='p', phase='started'))
        update_runtime_fact_terminal(RuntimeFactTerminalRequest(root=root, request_id=name, status='failed'))
    else:
        write_runtime_fact_source(RuntimeFactSourceRequest(root=root, request_id=name, user_prompt='p', phase=phase,
                                                           status='DONE' if phase == 'final' else 'running'))
    if age_days:
        stamp = time.time() - age_days * DAY
        os.utime(facts_dir(home) / name / 'task.json', (stamp, stamp))
    return facts_dir(home) / name


def fill_locator(home):
    # 写入12条更新的事实，把被测事实挤出最近候选页（locator只保护最新12条）。
    return [fact(home, f'recent-{index:02d}') for index in range(12)]


def request(home, *, cursor=None, items=STORAGE_LANE_SLICE_ENTRIES, hold=False, commit=None, now=None):
    return StorageLaneRequest('tool_outputs', home, now or time.time(), cursor or {},
                              StorageLaneBudget(items, None, None), hold, commit is not None, None, commit)


def drain(home, *, cursor=None, limit=20, **kwargs):
    rows = []
    for _ in range(limit):
        result = retention.maintain_owner_tool_outputs_lane(request(home, cursor=cursor, **kwargs))
        validate_lane_receipt(result)
        rows.append(result)
        cursor = json.loads(json.dumps(result['cursor']))
        if result['complete']:
            return rows
    pytest.fail('runtime facts pass did not finish: ' + repr(rows[-1]))


def test_retires_only_old_terminal_runtime_facts(home, tmp_path):
    old = {name: fact(home, name, phase=phase, age_days=40) for name, phase in
           (('old-final', 'final'), ('old-failed', 'failed'), ('old-live', 'started'), ('old-approved', 'approved'))}
    young = [fact(home, f'young-{index}') for index in range(12)]
    protected = fact(home, 'protected-final', age_days=40)
    outside = tmp_path / 'outside-final'
    outside.mkdir()
    (outside / 'task.json').write_text(json.dumps({'source': 'runtime_fact_source', 'runtime_progress': {'phase': 'final'}}))
    stamp = time.time() - 40 * DAY
    os.utime(outside / 'task.json', (stamp, stamp))
    os.symlink(outside, facts_dir(home) / 'linked-final')
    rows = drain(home)
    assert sum(row.get('deleted_items', 0) for row in rows) == 2
    assert not old['old-final'].exists() and not old['old-failed'].exists()
    assert old['old-live'].exists() and old['old-approved'].exists() and protected.exists()
    assert all(path.exists() for path in young)
    assert (outside / 'task.json').exists() and (facts_dir(home) / 'linked-final').is_symlink()


def test_legal_hold_and_override_keep_facts(home):
    target = fact(home, 'held', age_days=200)
    fill_locator(home)
    assert retention.maintain_owner_tool_outputs_lane(request(home, hold=True))['deferred_reason'] == 'legal_hold'
    assert target.exists()
    Path(home.owner_retention_json).write_text(json.dumps({'tool_output_days': 365}))
    drain(home)
    assert target.exists()
    Path(home.owner_retention_json).write_text(json.dumps({'tool_output_days': 100}))
    drain(home)
    assert not target.exists()


def test_bounded_slices_resume_and_rest(home, monkeypatch):
    monkeypatch.setattr(StorageLaneBudget, 'remaining_ms', lambda self: 1000.0)
    for index in range(300):
        fact(home, f'old-{index:03d}', age_days=40)
    for index in range(40):
        fact(home, f'new-{index:03d}')
    rows = drain(home)
    assert len(rows) >= 3
    assert all(row['items'] <= STORAGE_LANE_SLICE_ENTRIES for row in rows)
    assert all(not row['complete'] for row in rows[:-1])
    assert sum(row.get('deleted_items', 0) for row in rows) == 300
    remaining = {path.name for path in facts_dir(home).iterdir()}
    assert {f'new-{index:03d}' for index in range(40)} <= remaining
    assert not any(name.startswith('old-') for name in remaining)
    cursor = rows[-1]['cursor']
    assert cursor['facts_after'] == '' and cursor['facts_due_at'] > time.time()
    fact(home, 'old-late', age_days=40)
    rested = retention.maintain_owner_tool_outputs_lane(request(home, cursor=cursor))
    assert rested['items'] == 0 and (facts_dir(home) / 'old-late').exists()


def test_kept_aged_entries_do_not_pin_the_pass(home, monkeypatch):
    # 只验证条数上限与续点推进；真实时间门由预算耗尽用例覆盖，避免结果随机器快慢变化。
    monkeypatch.setattr(StorageLaneBudget, 'remaining_ms', lambda self: 1000.0)
    for index in range(2 * STORAGE_LANE_SLICE_ENTRIES + 5):
        fact(home, f'approved-{index:03d}', phase='approved', age_days=40)
    rows = drain(home, limit=5)
    assert rows[-1]['complete'] and sum(row.get('deleted_items', 0) for row in rows) == 0


def test_deletions_only_inside_run_write_and_busy_lock_skips(home, monkeypatch):
    busy = fact(home, 'busy', age_days=40)
    free = fact(home, 'free', age_days=40)
    fill_locator(home)
    inside, original = [False], retention._retire_fact_dir

    def commit(operation):
        inside[0] = True
        try:
            return operation()
        finally:
            inside[0] = False

    def guarded(*args):
        assert inside[0]
        return original(*args)

    monkeypatch.setattr(retention, '_retire_fact_dir', guarded)
    with held(busy / 'task.json'):
        drain(home, commit=commit)
    assert busy.exists() and not free.exists()


def test_resting_receipt_unchanged_without_facts_and_no_dir_fd_is_noop(home, monkeypatch):
    now = time.time()
    resting = {'next_pass_at': now + 100}
    assert retention.maintain_owner_tool_outputs_lane(request(home, cursor=resting, now=now)) == {
        'cursor': resting, 'complete': True, 'items': 0, 'bytes': 0}
    target = fact(home, 'old', age_days=40)
    fill_locator(home)
    monkeypatch.setattr(nofollow_fs, '_supports_dir_fd', lambda: False)
    result = retention.maintain_owner_tool_outputs_lane(request(home, cursor=resting, now=now))
    assert result == {'cursor': resting, 'complete': True, 'items': 0, 'bytes': 0} and target.exists()


@pytest.mark.parametrize('phase', ['started', 'tool_loop', 'running', 'unknown', None, 0, [], {}])
def test_nonterminal_or_malformed_phases_are_preserved(home, phase):
    target = fact(home, 'uncertain-phase', age_days=40)
    task = target / 'task.json'
    payload = json.loads(task.read_text())
    payload['runtime_progress']['phase'] = phase
    task.write_text(json.dumps(payload), encoding='utf-8')
    stamp = time.time() - 40 * DAY
    os.utime(task, (stamp, stamp))
    fill_locator(home)

    result = retention.maintain_owner_tool_outputs_lane(request(home))

    validate_lane_receipt(result)
    assert target.exists() and json.loads(task.read_text())['runtime_progress']['phase'] == phase
    assert result.get('deleted_items', 0) == 0 and not result.get('errors')


def test_storage_imports_in_a_fresh_process(tmp_path):
    # pytest 收集可能已预热 memory_store；独立解释器才能捕获首次导入循环。
    result = subprocess.run([sys.executable, '-c', 'import agent_py_agent.agent.memory_archive.storage'],
        cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(Path(retention.__file__).resolve().parents[3])},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('exhausted', [False, True])
def test_saved_cursor_progresses_after_production_size_prefix_seek(home, monkeypatch, exhausted):
    target = fact(home, 'expired-tail', age_days=40)
    fill_locator(home)
    identity = target.parent.stat().st_ino
    scan = os.scandir
    clock, visited = [100.0], [0]
    prefix_count = 100_000
    seconds_per_entry = 0.00000025
    anchor = f'kept-{prefix_count - 1:06d}'

    @contextmanager
    def prefixed_scan(path):
        with scan(path) as entries:
            if isinstance(path, int) and os.fstat(path).st_ino == identity:
                real_entries = sorted(entries, key=lambda entry: entry.name != target.name)

                def stream():
                    for index in range(prefix_count):
                        clock[0] += seconds_per_entry
                        visited[0] += 1
                        yield SimpleNamespace(name=f'kept-{index:06d}')
                    yield from real_entries

                yield stream()
            else:
                yield entries

    # 只模拟生产量级前缀的25ms成本；真实128项/12.5ms预算计算保持启用，不声称这是磁盘延迟测量。
    observed_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    observed_os.scandir = prefixed_scan
    monkeypatch.setattr(retention, 'os', observed_os)
    monkeypatch.setattr(storage_lanes, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    cursor = {'facts_after': anchor, 'next_pass_at': time.time() + 3600}
    receipts = []
    for _ in range(4):
        req = request(home, cursor=cursor)
        if exhausted:
            req = replace(req, budget=StorageLaneBudget(STORAGE_LANE_SLICE_ENTRIES, None, clock[0] - 1))
        result = retention.maintain_owner_tool_outputs_lane(req)
        validate_lane_receipt(result)
        receipts.append(result)
        cursor = json.loads(json.dumps(result['cursor']))
    assert all(row['items'] <= STORAGE_LANE_SLICE_ENTRIES for row in receipts)
    if exhausted:
        assert visited[0] == 0 and target.exists()
        assert all(row['items'] == 0 and not row['complete'] for row in receipts)
        assert cursor['facts_after'] == anchor
    else:
        assert visited[0] >= prefix_count
        assert any(row['items'] > 0 or row['cursor'].get('facts_after') != anchor for row in receipts)
        assert not target.exists() and any(row['complete'] for row in receipts)


@contextmanager
def held(path):
    ready, release = threading.Event(), threading.Event()

    def worker():
        with locked_json_path(path):
            ready.set()
            assert release.wait(10)

    thread = threading.Thread(target=worker)
    thread.start()
    assert ready.wait(5)
    try:
        yield
    finally:
        release.set()
        thread.join(5)
