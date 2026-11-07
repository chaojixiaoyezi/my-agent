"""同一压缩头连续完整性失败达到阈值后，经原 CAS 恢复原文。"""
from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.runtime import context_compactor
from agent_py_agent.agent.conversation import compact, compact_summary_view
from agent_py_agent.tests.test_res_w3_compact_head_reset import (
    _assert_reset,
    _commit,
    _load,
    _seed,
    _warnings,
)
from agent_py_agent.tests.test_res_w3_compact_head_reset import compact_case as _compact_case

compact_case = _compact_case


def _fail_resolutions(monkeypatch, failures):
    original = compact_summary_view.resolve_compact_summary_view
    calls = []

    def resolve(agent, thread, scope):
        calls.append(thread)
        if len(calls) in failures:
            raise OSError('conversation compact checkpoint content identity mismatches')
        return original(agent, thread, scope)

    monkeypatch.setattr(compact_summary_view, 'resolve_compact_summary_view', resolve)
    return calls


def _compact_files(case):
    root = case.root / 'compact'
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_below_threshold_keeps_thread_unchanged_without_writes(compact_case, monkeypatch, caplog):
    case = compact_case
    before = _commit(case, _seed(case))
    path = case.store.storage.thread_path(case.thread_id)
    raw, files = path.read_bytes(), _compact_files(case)
    _fail_resolutions(monkeypatch, {1, 2})
    monkeypatch.setattr(case.store.threads, 'update_atomic', lambda *_args, **_kwargs: pytest.fail('阈值前不得写线程'))
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(OSError, match='content identity mismatches'):
                _load(case, before)
            assert path.read_bytes() == raw
    assert _compact_files(case) == files
    assert not _warnings(caplog, 'COMPACT_HEAD_RESET')


def test_threshold_resets_head_once_and_preserves_checkpoint_files(compact_case, monkeypatch, caplog):
    case = compact_case
    rows = _seed(case)
    before = _commit(case, rows)
    files = _compact_files(case)
    calls = _fail_resolutions(monkeypatch, {1, 2, 3})
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            with pytest.raises(OSError, match='content identity mismatches'):
                _load(case, before)
        source = _load(case, before)
    assert list(source.messages) == rows
    _assert_reset(case, before, source)
    assert len(_warnings(caplog, 'COMPACT_HEAD_RESET')) == 1
    assert _compact_files(case) == files
    assert len(calls) == 4 and not calls[-1].compact_checkpoint_id
    assert not any(key[0] == case.thread_id for key in compact._compact_integrity_failures)


def test_success_restarts_consecutive_failure_count(compact_case, monkeypatch, caplog):
    case = compact_case
    rows = _seed(case)
    before = _commit(case, rows)
    path = case.store.storage.thread_path(case.thread_id)
    raw = path.read_bytes()
    _fail_resolutions(monkeypatch, {1, 2, 4, 5})
    with caplog.at_level(logging.WARNING):
        for index in range(1, 6):
            if index == 3:
                assert list(_load(case, before).messages) == rows[4:]
            else:
                with pytest.raises(OSError, match='content identity mismatches'):
                    _load(case, before)
    assert path.read_bytes() == raw
    assert not _warnings(caplog, 'COMPACT_HEAD_RESET')


def test_zero_threshold_never_resets(compact_case, monkeypatch, caplog):
    case = compact_case
    before = _commit(case, _seed(case))
    path = case.store.storage.thread_path(case.thread_id)
    raw, files = path.read_bytes(), _compact_files(case)
    policy = context_compactor.runtime_compact_policy
    monkeypatch.setattr(context_compactor, 'runtime_compact_policy',
                        lambda *args, **kwargs: replace(policy(*args, **kwargs), failure_threshold=0))
    _fail_resolutions(monkeypatch, {1, 2, 3, 4, 5})
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            with pytest.raises(OSError, match='content identity mismatches'):
                _load(case, before)
    assert path.read_bytes() == raw and _compact_files(case) == files
    assert not _warnings(caplog, 'COMPACT_HEAD_RESET')
