# LLM: PERF-W2 回归：维护拍不得每60秒整表重解析候选账本；终态冷归档按策展周期节流，失败下一拍立即重试。
# 模块用途: 固定 run_if_due 的终态归档节流与失败重试合同。
from __future__ import annotations

import pytest

from agent_py_agent.agent.memory_store.curator import CuratorInputError
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
from agent_py_agent.tests.test_memory_curator_v2 import (
    _conversation,
    _service,
    _StaticStructuredBackend,
)


# LLM: 大间隔与高轮次阈值让本测试只观察维护段，不依赖模型批次是否到期。
# 函数用途: 构造一个带计数桩的策展服务。
def _counting_service(tmp_path, monkeypatch, *, fail_first=False):
    store, _thread, _message = _conversation(tmp_path)
    config = MemoryCuratorConfig(interval_seconds=3600, turn_threshold=1000, timeout_seconds=2, max_retries=0)
    service = _service(tmp_path, _StaticStructuredBackend({"schema_version": "wrong"}), store, config=config)
    calls = []

    def compact(**kwargs):
        calls.append(kwargs)
        if fail_first and len(calls) == 1:
            raise OSError("archive write failed")
        return 0

    monkeypatch.setattr(service.candidate_service, "compact_terminal_candidates", compact)
    return service, calls


def test_terminal_compaction_runs_once_per_curator_interval(tmp_path, monkeypatch):
    service, calls = _counting_service(tmp_path, monkeypatch)
    service.run_if_due()
    service.run_if_due()
    assert len(calls) == 1
    service._terminal_compaction_at -= 3601
    service.run_if_due()
    assert len(calls) == 2


def test_failed_terminal_compaction_is_retried_next_tick(tmp_path, monkeypatch):
    service, calls = _counting_service(tmp_path, monkeypatch, fail_first=True)
    with pytest.raises(CuratorInputError):
        service.run_if_due()
    service.run_if_due()
    assert len(calls) == 2
