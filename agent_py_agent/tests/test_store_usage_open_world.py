"""C7 加固：持久用量账的用途桶改成开放世界（store_usage.py）。

锁定：
- 读取端接受不认识的用途键：能读、能求和、键原样保留；三条严格规则（值必须是对象、
  不能嵌套 purpose_breakdown、schema 版本不对）仍然 fail closed；
- 写入端 probe:tool_capability 只在真有探测用量时落键，老三个桶照写。
"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.store_usage import (
    _PURPOSE_SCHEMA,
    _purpose_rows,
    _sum_purpose_breakdowns,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError


# 函数用途: 生成一个用途桶的最小累计形状（数字字段按需给，缺省为 0）。
def _bucket(**counts: int) -> dict:
    physical = counts.get("physical", 0)
    input_tokens = counts.get("input", 0)
    output_tokens = counts.get("output", 0)
    return {
        "schema": "model_call_summary.v1",
        "logical_model_turn_count": counts.get("logical", 0),
        "physical_model_attempt_count": physical,
        "model_retry_count": 0,
        "provider_http_attempt_count": physical,
        "provider_http_retry_count": 0,
        "status_counts": {"finished": physical},
        "backends": ["test"],
        "models": ["test-model"],
        "accounted_input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "provider_usage_call_count": physical,
        "estimated_usage_call_count": 0,
        "usage_breakdown": {
            "schema": "model_usage_breakdown.v1",
            "provider": {},
            "estimated": {},
        },
    }


# 函数用途: 生成带指定用途桶的累计快照（根累计取最小形状）。
def _calls_with_purposes(purposes: dict) -> dict:
    return {
        "schema": "model_call_summary.v1",
        "physical_model_attempt_count": 1,
        "provider_usage_call_count": 1,
        "status_counts": {"finished": 1},
        "backends": ["test"],
        "models": ["test-model"],
        "accounted_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "estimated_usage_call_count": 0,
        "usage_breakdown": {
            "schema": "model_usage_breakdown.v1",
            "provider": {},
            "estimated": {},
        },
        "purpose_breakdown": {"schema": _PURPOSE_SCHEMA, **purposes},
    }


def test_read_accepts_unknown_purpose_keys_sums_and_preserves_them():
    first = _calls_with_purposes({
        "main": _bucket(logical=1, physical=1, input=10, output=2),
        "future:archival": _bucket(logical=1, physical=1, output=7),
    })
    second = _calls_with_purposes({
        "main": _bucket(logical=1, physical=1, input=5, output=3),
        "future:archival": _bucket(logical=1, physical=1, output=1),
    })
    total = _sum_purpose_breakdowns([first, second])
    assert total["schema"] == _PURPOSE_SCHEMA
    # 已知键照常求和，未知键原样保留并参与求和
    assert total["main"]["output_tokens"] == 5
    assert total["main"]["physical_model_attempt_count"] == 2
    assert total["future:archival"]["output_tokens"] == 8
    assert total["future:archival"]["physical_model_attempt_count"] == 2
    # 已知三桶 + probe 桶 + 未知键都出现在结果里（已知桶空结构也保留）
    assert set(total) == {
        "schema", "main", "auxiliary", "decision",
        "probe:tool_capability", "future:archival",
    }


def test_unknown_purpose_row_reads_through_store_summary(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "open-world-reader"})
    row = _calls_with_purposes({
        "main": _bucket(output=2),
        "future:archival": _bucket(output=7),
    })
    store.model_usage.append_once({
        "event_id": "usage-open-world-1",
        "thread_id": thread.thread_id,
        "request_id": "req",
        "run_id": "run",
        "source": "test",
        "model_calls": row,
    })
    rows, errors = store.model_usage.events_report(thread.thread_id)
    assert not errors and len(rows) == 1
    summary = store.model_usage.summary(thread.thread_id)
    purposes = summary["purpose_breakdown"]
    assert purposes["main"]["output_tokens"] == 2
    assert purposes["future:archival"]["output_tokens"] == 7, "未知用途键经 summary 原样保留"


def test_write_without_probe_usage_omits_probe_key(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "write-no-probe"})
    calls = _calls_with_purposes({
        "main": _bucket(logical=1, physical=1, input=10, output=2),
        "auxiliary": _bucket(),
        "decision": _bucket(),
    })
    event = store.model_usage.append_snapshot_once({
        "thread_id": thread.thread_id,
        "request_id": "req",
        "run_id": "run",
        "source": "test",
        "model_calls": calls,
    })
    purposes = event.model_calls["purpose_breakdown"]
    assert "probe:tool_capability" not in purposes, "没有探测用量时不许写空 probe 桶"
    assert {"main", "auxiliary", "decision"} <= set(purposes), "老三个桶照写"


def test_write_with_probe_usage_includes_probe_key(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "write-with-probe"})
    calls = _calls_with_purposes({
        "main": _bucket(logical=1, physical=1, output=2),
        "probe:tool_capability": _bucket(logical=1, physical=1, output=12),
    })
    event = store.model_usage.append_snapshot_once({
        "thread_id": thread.thread_id,
        "request_id": "req",
        "run_id": "run",
        "source": "test",
        "model_calls": calls,
    })
    purposes = event.model_calls["purpose_breakdown"]
    assert purposes["probe:tool_capability"]["output_tokens"] == 12
    assert purposes["probe:tool_capability"]["physical_model_attempt_count"] == 1


def test_strict_purpose_rules_still_fail_closed():
    # schema 版本不对
    bad_schema = _calls_with_purposes({"main": _bucket()})
    bad_schema["purpose_breakdown"]["schema"] = "model_call_purpose_breakdown.v9"
    with pytest.raises(DataCorruptionError, match="purpose breakdown is invalid"):
        _purpose_rows(bad_schema)
    # 桶值不是对象
    not_object = _calls_with_purposes({"main": "boom"})
    with pytest.raises(DataCorruptionError, match="purpose bucket is invalid"):
        _purpose_rows(not_object)
    # 桶值嵌套 purpose_breakdown
    nested = _calls_with_purposes({"main": {**_bucket(), "purpose_breakdown": {}}})
    with pytest.raises(DataCorruptionError, match="purpose bucket is invalid"):
        _purpose_rows(nested)
    # 根值不是对象（连 schema 都没有）
    flat = {"main": 1}
    with pytest.raises(DataCorruptionError, match="purpose breakdown is invalid"):
        _purpose_rows({"purpose_breakdown": flat})
    # 未知用途键的桶值也必须合规：字符串桶值照样报错，不能因开放世界放行
    unknown_bad = _calls_with_purposes({"future:archival": "bad"})
    with pytest.raises(DataCorruptionError, match="purpose bucket is invalid"):
        _purpose_rows(unknown_bad)