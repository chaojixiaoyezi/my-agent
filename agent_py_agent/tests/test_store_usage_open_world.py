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


# LLM: 生产快照的用途桶来自 model_call_ledger._ModelCallAggregate.to_summary()，四个桶固定都在；
#   没有探测时 probe 桶是全 0 骨架字典（非空），按字典真假判断会误写空键，所以用例用真实账本形状锁定。
# 函数用途: 回归锁 M1：真实账本形状（只有 main 调用）写一行时不得出现 probe 键。
def test_write_with_real_ledger_shape_omits_probe_key(tmp_path):
    from agent_py_agent.agent.contracts.model_call_ledger import (
        ModelCallRecord,
        _ModelCallAggregate,
    )

    aggregate = _ModelCallAggregate()
    aggregate.observe_new(ModelCallRecord(
        call_id="call-main-1",
        backend="test-backend",
        model="test-model",
        input_tokens=10,
        request_id="req",
        run_id="run",
        status="finished",
        output_tokens=2,
        provider_usage_reported=True,
    ))
    # 生产组装与 call_runtime.model_call_summary 一致：根带 schema，用途桶来自真实账本 to_summary()
    calls = {"schema": "model_call_summary.v1", **aggregate.to_summary()}
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "write-real-ledger"})
    event = store.model_usage.append_snapshot_once({
        "thread_id": thread.thread_id,
        "request_id": "req",
        "run_id": "run",
        "source": "test",
        "model_calls": calls,
    })
    purposes = event.model_calls["purpose_breakdown"]
    assert "probe:tool_capability" not in purposes, "真实账本形状（probe 桶全 0 骨架）不许写空 probe 键"
    assert {"main", "auxiliary", "decision"} <= set(purposes), "老三个桶照写"


# LLM: 同一范围第二行起，先前累计由 _sum_model_call_summaries 求和；无探测时求和没有 probe 键，
#   但上一版"字典真假"判断在第二行会因 prior 求和骨架而误写，本用例锁两行都不写。
# 函数用途: 回归锁 M1 场景 (b)：同范围两行都没有探测时，两行都不许出现 probe 键。
def test_write_two_rows_without_probe_usage_omits_probe_key_in_both(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "write-two-no-probe"})
    scope = {
        "thread_id": thread.thread_id,
        "request_id": "req",
        "run_id": "run",
        "source": "test",
    }
    first_calls = _calls_with_purposes({
        "main": _bucket(logical=1, physical=1, input=10, output=2),
        "auxiliary": _bucket(),
        "decision": _bucket(),
    })
    first = store.model_usage.append_snapshot_once({**scope, "model_calls": first_calls})
    second_calls = _calls_with_purposes({
        "main": _bucket(logical=2, physical=2, input=15, output=3),
        "auxiliary": _bucket(),
        "decision": _bucket(),
    })
    second = store.model_usage.append_snapshot_once({**scope, "model_calls": second_calls})
    for event in (first, second):
        purposes = event.model_calls["purpose_breakdown"]
        assert "probe:tool_capability" not in purposes, "两行都没有探测时第二行也不许写空 probe 键"


# LLM: 探测从无到有再到继续累计：probe 键只在出现用量那行才落，续写行按增量减 prior，
#   各行增量求和应等于最后快照的累计值；判断"有没有"只看结构化计数，不因 prior 有骨架就倒退。
# 函数用途: 回归锁 M1 场景 (c)：先无后有再续写，probe 键出现时机与增量累计都对。
def test_write_probe_appears_after_usage_and_deltas_sum_to_cumulative(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "write-probe-later"})
    scope = {
        "thread_id": thread.thread_id,
        "request_id": "req",
        "run_id": "run",
        "source": "test",
    }
    no_probe = _calls_with_purposes({
        "main": _bucket(logical=1, physical=1, input=10, output=2),
        "auxiliary": _bucket(),
        "decision": _bucket(),
    })
    first = store.model_usage.append_snapshot_once({**scope, "model_calls": no_probe})
    assert "probe:tool_capability" not in first.model_calls["purpose_breakdown"]
    with_probe = _calls_with_purposes({
        "main": _bucket(logical=2, physical=2, input=15, output=3),
        "auxiliary": _bucket(),
        "decision": _bucket(),
        "probe:tool_capability": _bucket(logical=1, physical=1, output=12),
    })
    second = store.model_usage.append_snapshot_once({**scope, "model_calls": with_probe})
    second_probe = second.model_calls["purpose_breakdown"]["probe:tool_capability"]
    assert second_probe["physical_model_attempt_count"] == 1
    assert second_probe["output_tokens"] == 12
    with_more_probe = _calls_with_purposes({
        "main": _bucket(logical=3, physical=3, input=20, output=4),
        "auxiliary": _bucket(),
        "decision": _bucket(),
        "probe:tool_capability": _bucket(logical=2, physical=2, output=20),
    })
    third = store.model_usage.append_snapshot_once({**scope, "model_calls": with_more_probe})
    third_probe = third.model_calls["purpose_breakdown"]["probe:tool_capability"]
    assert third_probe["physical_model_attempt_count"] == 1, "第三行增量 = 2 - 1"
    assert third_probe["output_tokens"] == 8, "第三行增量 = 20 - 12"
    total = _sum_purpose_breakdowns([second.model_calls, third.model_calls])
    probe_total = total["probe:tool_capability"]
    assert probe_total["physical_model_attempt_count"] == 2, "各行增量加起来等于累计"
    assert probe_total["output_tokens"] == 20