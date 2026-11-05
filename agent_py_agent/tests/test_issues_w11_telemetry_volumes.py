"""W11：通过真实写账、跨进程锁及读索引验证遥测分卷，不使用生产 owner。"""
from __future__ import annotations

import json
import multiprocessing
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_py_agent.agent.observability import telemetry_ledger as module
from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger


@pytest.fixture
# LLM: 仅缩小本测试进程的容量阈值，monkeypatch自动还原。
# 函数用途: 用小文件覆盖真实封卷和退休。
def small_volumes(monkeypatch):
    monkeypatch.setattr(module, 'TELEMETRY_VOLUME_MAX_BYTES', 4096, raising=False)
    monkeypatch.setattr(module, 'TELEMETRY_TOTAL_MAX_BYTES', 16384, raising=False)


# LLM: 调用真实账本写入接口，不绕过锁或伪造落盘状态。
# 函数用途: 写入带唯一调用号的测试记录。
def _record(ledger, name):
    return ledger.record_tool_call(dict(call_id=name, tool_name='read', task_id='task',
                                       args_summary={'text': 'x' * 160}, started_at=1, ended_at=2))


# LLM: 测试专用全集核对，覆盖活动卷和封卷，不作为业务查询实现。
# 函数用途: 读取实际保留下来的所有行。
def _rows(home):
    return [json.loads(line) for path in (home / 'data/telemetry').glob('tool_calls*.jsonl')
            for line in path.read_text().splitlines()]


# LLM: spawn子进程重新设置小阈值，总量留足避免把合法退休误判为丢行。
# 函数用途: 跨进程追加独立的一组调用。
def _spawn_writer(home):
    module.TELEMETRY_VOLUME_MAX_BYTES = 4096
    module.TELEMETRY_TOTAL_MAX_BYTES = 1024 * 1024
    for i in range(20):
        _record(ToolTelemetryLedger(home), f'process-{i}')


# LLM: 每次写后核对活动卷和最终真实总字节，不仅检查文件存在。
# 函数用途: 验证连续追加会封卷且容量有界。
def test_real_appends_seal_and_bound_total(tmp_path, small_volumes):
    ledger = ToolTelemetryLedger(tmp_path)
    active = tmp_path / 'data/telemetry/tool_calls.jsonl'
    for i in range(90):
        _record(ledger, str(i))
        assert active.is_file()
    sealed = [p for p in active.parent.iterdir() if p.name != active.name and p.suffix == '.jsonl']
    assert len(sealed) >= 2
    assert all(re.fullmatch(r'tool_calls\.\d{19}\.jsonl', p.name) for p in sealed)
    assert sum(p.stat().st_size for p in [active, *sealed]) <= 16384 + 4096
    assert (active.parent / '.telemetry.lock').exists()


# LLM: 预置精确到期、未到期及非普通文件候选，验证不误删资产。
# 函数用途: 验证时间退休及精确名称保护。
def test_retirement_ttl_only_touches_regular_matching_sealed_files(tmp_path, small_volumes):
    ledger = ToolTelemetryLedger(tmp_path)
    _record(ledger, 'seed')
    root = tmp_path / 'data/telemetry'
    now = time.time()
    old = root / f'tool_calls.{int((now - 31 * 86400) * 1e9):019d}.jsonl'
    kept = root / f'tool_calls.{int((now - 29 * 86400) * 1e9):019d}.jsonl'
    old.write_text('old')
    kept.write_text('kept')
    unrelated = root / 'user.jsonl'
    unrelated.write_text('asset')
    directory = root / f'tool_calls.{time.time_ns():019d}.jsonl'
    directory.mkdir()
    module._NEXT_RETENTION_CHECK[str(tmp_path)] = 0
    _record(ledger, 'check')
    assert not old.exists() and kept.read_text() == 'kept'
    assert unrelated.read_text() == 'asset' and directory.is_dir()


# LLM: 复用真实读索引，必须换代且require_complete通过，不修改索引实现。
# 函数用途: 验证三类查询在封卷后只覆盖活动卷。
def test_query_generation_after_seal_covers_only_active(tmp_path, small_volumes, monkeypatch):
    ledger = ToolTelemetryLedger(tmp_path)
    _record(ledger, 'old')
    before = ledger.query_tool_calls_page()['view']['generation']
    monkeypatch.setattr(module, 'TELEMETRY_VOLUME_MAX_BYTES', 1)
    _record(ledger, 'sealed')
    monkeypatch.setattr(module, 'TELEMETRY_VOLUME_MAX_BYTES', 4096)
    _record(ledger, 'active')
    page = module.require_complete(ledger.query_tool_calls_page())
    assert page['view']['generation'] != before
    assert [r['call_id'] for r in ledger.query_tool_calls()] == ['active']
    assert ledger.get_tool_call('active')['call_id'] == 'active'
    assert ledger.get_tool_call('old') is None
    assert ledger.aggregate_tool_stats()['total_calls'] == 1


# LLM: 精确核对全部调用号计数；不以子进程启动或退出代替不丢行证据。
# 函数用途: 验证原跨进程锁保护并发追加与封卷。
def test_two_threads_and_spawn_process_keep_every_row(tmp_path, small_volumes, monkeypatch):
    monkeypatch.setattr(module, 'TELEMETRY_TOTAL_MAX_BYTES', 1024 * 1024)
    ctx = multiprocessing.get_context('spawn')
    child = ctx.Process(target=_spawn_writer, args=(tmp_path,))
    child.start()
    try:
        def thread_writer(prefix):
            for i in range(20):
                _record(ToolTelemetryLedger(tmp_path), f'{prefix}-{i}')
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(thread_writer, ('first', 'second')))
        child.join(timeout=30)
        assert not child.is_alive() and child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
    counts = Counter(row['call_id'] for row in _rows(tmp_path))
    assert counts == Counter(f'{prefix}-{i}' for prefix in ('first', 'second', 'process') for i in range(20))


# LLM: 故障只注入封卷rename；业务已提交的行和告警必须同时可见。
# 函数用途: 验证封卷故障不会把成功写账变成工具失败。
def test_rename_failure_does_not_fail_already_committed_append(tmp_path, small_volumes, monkeypatch, caplog):
    from types import SimpleNamespace
    monkeypatch.setattr(module, 'TELEMETRY_VOLUME_MAX_BYTES', 1)
    def refuse(*args, **kwargs):
        raise OSError('synthetic rename failure')
    monkeypatch.setattr(module, 'os', SimpleNamespace(**{**vars(module.os), 'rename': refuse}))
    ledger = ToolTelemetryLedger(tmp_path)
    for i in range(4):
        _record(ledger, f'kept-{i}')
    assert [row['call_id'] for row in ledger.query_tool_calls()] == [f'kept-{i}' for i in range(4)]
    assert '遥测' in caplog.text


# LLM: 检查期限内的普通追加若列目录即失败，不用耗时波动推断复杂度。
# 函数用途: 验证热路径跳过卷扫描。
def test_append_does_not_list_volumes_before_next_check(tmp_path, small_volumes, monkeypatch):
    ledger = ToolTelemetryLedger(tmp_path)
    _record(ledger, 'first')
    def no_listing(*args, **kwargs):
        raise AssertionError('unexpected directory listing on append')
    monkeypatch.setattr(module, 'list_names_beneath', no_listing)
    _record(ledger, 'second')
    assert ledger.get_tool_call('second') is not None


# LLM: 超过预算的到期卷和指向资产的链接并存，逐个核对实际删除量。
# 函数用途: 验证最多128次删除且不跟随软链接。
def test_retirement_unlink_budget_and_symlink_protection(tmp_path, small_volumes):
    ledger = ToolTelemetryLedger(tmp_path)
    _record(ledger, 'seed')
    root = tmp_path / 'data/telemetry'
    stamp = int((time.time() - 31 * 86400) * 1e9)
    old = [root / f'tool_calls.{stamp + i:019d}.jsonl' for i in range(130)]
    for path in old:
        path.write_text('old')
    protected = tmp_path / 'asset'
    protected.write_text('preserve')
    link = root / f'tool_calls.{stamp - 1:019d}.jsonl'
    link.symlink_to(protected)
    module._NEXT_RETENTION_CHECK[str(tmp_path)] = 0
    _record(ledger, 'check')
    assert sum(not path.exists() for path in old) == 128
    assert link.is_symlink() and protected.read_text() == 'preserve'


# LLM: 只故障注入本模块的目录fsync，不影响已提交追加及原nofollow辅助的文件同步。
# 函数用途: 验证封卷rename成功后目录同步失败也留下可查询的新活动卷。
def test_directory_fsync_failure_preserves_active_after_seal(tmp_path, small_volumes, monkeypatch, caplog):
    import stat
    from types import SimpleNamespace

    original = module.os
    def fail_directory_sync(descriptor):
        assert stat.S_ISDIR(original.fstat(descriptor).st_mode)
        raise OSError('synthetic directory fsync failure')

    monkeypatch.setattr(module, 'os', SimpleNamespace(**{**vars(original), 'fsync': fail_directory_sync}))
    monkeypatch.setattr(module, 'TELEMETRY_VOLUME_MAX_BYTES', 1)
    ledger = ToolTelemetryLedger(tmp_path)
    assert _record(ledger, 'committed').call_id == 'committed'
    active = tmp_path / 'data/telemetry/tool_calls.jsonl'
    assert active.is_file()
    assert module.require_complete(ledger.query_tool_calls_page())['total_count'] == 0
    assert [row['call_id'] for row in _rows(tmp_path)] == ['committed']
    assert '遥测' in caplog.text
