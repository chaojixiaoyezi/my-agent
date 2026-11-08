# LLM: 使用真实事实写者和临时Owner目录；维护只经既有tool_outputs通道和协调器写门。
# 模块用途: 验证运行事实到期回收的保护条件、分片预算及静息回执兼容。
import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_py_agent.agent.common import nofollow_fs
from agent_py_agent.agent.common.json_io import locked_json_path, write_private_text_file_atomic
from agent_py_agent.agent.memory_archive import tool_output_retention as retention
from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    ApprovedRuntimeFactSourceRequest,
    RuntimeFactSourceRequest,
    RuntimeFactTerminalRequest,
    update_runtime_fact_terminal,
    write_approved_runtime_fact_source,
    write_runtime_fact_source,
)
from agent_py_agent.agent.run_intent import _MAX_FACT_FILES
from agent_py_agent.agent.run_intent_index import _locator_path, read_locator
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.storage_lanes import (
    STORAGE_LANE_SLICE_ENTRIES,
    StorageLaneBudget,
    StorageLaneRequest,
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


def fact(owner, name, *, phase='final', age=31, approved=False, root=None):
    home, now = owner
    root = Path(root or home.owner_home_dir)
    directory = Path(write_runtime_fact_source(RuntimeFactSourceRequest(
        root=root, request_id=name, status='DONE' if phase == 'final' else 'running')))
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
    write_private_text_file_atomic(policy, json.dumps({'tool_output_days': 365}))
    # 事实遍历计入检查条数并在走完后记录休息点（scale-s8）；保留期覆盖仍使200天事实保留。
    kept = retention.maintain_owner_tool_outputs_lane(request(owner))
    assert kept['complete'] is True and kept['items'] == 1 and 'deleted_items' not in kept
    assert kept['cursor']['next_pass_at'] == owner[1] + 3600 and kept['cursor']['facts_after'] == ''
    assert directory.exists()
    write_private_text_file_atomic(policy, json.dumps({'tool_output_days': 30}))
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
