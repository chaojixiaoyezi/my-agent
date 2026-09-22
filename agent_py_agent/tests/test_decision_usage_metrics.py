"""原账本、用量存储与 TUI 决策分区的组合验证，不发真实模型请求。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    model_call_summary,
    record_model_call_finished,
)
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallStartedParams
from agent_py_agent.agent.conversation.model_metrics import (
    public_model_metrics,
    publish_model_metrics,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.cli.chat_parts.tui_markdown import display_width_text
from agent_py_agent.cli.chat_parts.tui_model_metrics import render_model_metrics
from agent_py_agent.tests.test_tui_model_metrics import fixture, settled


# LLM: 在同一原账本写入宿主决策用途与真实字段；不构造生成正文或用估算冒充供应商用量。
# 函数用途: 添加一条决策完成记录，供持久增量与显示验证。
def decision(agent, params, *, call_id="decision-1", usage=None):
    ledger = agent._model_call_ledger
    ledger.started(ModelCallStartedParams(call_id, "typesafe_decision", "systemone", 42,
        request_id=params.request_id, run_id=params.run_id,
        metadata={"purpose": "decision", "logical_call_id": call_id}))
    response = SimpleNamespace(usage={} if usage is None else usage)
    record_model_call_finished(ledger, call_id, response)
    return response


# LLM: 通过真实用量存储保存原累计快照，测试临时会话不影响日常配置。
# 函数用途: 使用正式增量入口提交当前 request/run 的账本事实。
def persist(agent, params, thread, summary=None):
    return agent.conversation_store.model_usage.append_snapshot_once({
        "thread_id": thread.thread_id, "request_id": params.request_id,
        "run_id": params.run_id, "task_id": "task", "source": "test",
        "model_calls": summary if summary is not None else model_call_summary(agent, request_id=params.request_id, run_id=params.run_id),
    })


def test_purpose_snapshot_delta_counts_input_once_and_keeps_real_output(tmp_path):
    agent, params, clock, _, thread = fixture(tmp_path)
    settled(agent, params, clock)
    first = persist(agent, params, thread)
    decision(agent, params, usage={"input_tokens": 12, "output_tokens": 3})
    second = persist(agent, params, thread)
    assert persist(agent, params, thread) == second
    assert second.event_id != first.event_id
    assert second.model_calls["physical_model_attempt_count"] == 1
    bucket = second.model_calls["purpose_breakdown"]["decision"]
    assert bucket["usage_breakdown"]["provider"]["input_tokens_reported_call_count"] == 1
    assert bucket["usage_breakdown"]["provider"]["input_tokens"] == 12
    total = agent.conversation_store.model_usage.summary(thread.thread_id)
    assert total["provider"]["input_tokens"] == 1012
    assert total["provider"]["output_tokens"] == 103
    assert total["purpose_totals_known"]
    assert total["purpose_breakdown"]["decision"]["output_tokens"] == 3


@pytest.mark.parametrize("usage,expected,complete,label", [
    ({"input_tokens": 0}, 0, True, "决策入 0"),
    ({"input_tokens": 12, "output_tokens": 3}, 12, True, "决策入 12"),
    ({"output_tokens": 3}, None, False, "决策入 未知"),
    ({}, None, False, "决策入 未知"),
])
def test_decision_metrics_preserve_main_rounds_cache_tools_and_unknown(tmp_path, usage, expected, complete, label):
    agent, params, clock, _, _ = fixture(tmp_path)
    response = settled(agent, params, clock)
    initial = publish_model_metrics(agent, params, pending=False, tool_count=4, response=response, call_id="call-1")
    response = decision(agent, params, usage=usage)
    metrics = publish_model_metrics(agent, params, pending=False, usage_only=True)
    for key in ("model_rounds", "tool_count", "pending", "cache_percent", "output_tps", "cache_diagnostic"):
        assert metrics.get(key) == initial.get(key)
    assert metrics["decision_call_count"] == 1
    assert metrics["decision_input_tokens"] == expected
    assert metrics["decision_input_complete"] is complete
    # 正常生成边界也不把决策算成生成轮；错误传入决策响应不污染最近生成缓存。
    metrics = publish_model_metrics(agent, params, pending=True, response=response, call_id="decision-1")
    assert metrics["model_rounds"] == 1 and metrics["cache_percent"] == initial["cache_percent"]
    text = "".join(part[1] for line in render_model_metrics(metrics, 250) for part in line)
    assert label in text and "决策出" not in text and "价格" not in text
    for width in (30, 80, 110, 180):
        rendered = "".join(part[1] for line in render_model_metrics(metrics, width) for part in line)
        assert display_width_text(rendered) <= width


def test_partial_decision_usage_is_not_hidden_by_complete_other_call(tmp_path):
    agent, params, _, _, _ = fixture(tmp_path)
    decision(agent, params, usage={"input_tokens": 12})
    decision(agent, params, call_id="decision-2", usage={"output_tokens": 5})
    metrics = publish_model_metrics(agent, params, pending=False)
    assert metrics["model_rounds"] == 0
    assert metrics["input_tokens"] == 12 and metrics["estimated_tokens"] == 42
    assert metrics["decision_input_tokens"] == 12 and not metrics["decision_input_complete"]
    text = "".join(part[1] for line in render_model_metrics(metrics, 250) for part in line)
    assert "决策入 12+?" in text


def test_original_baseline_refreshes_when_background_usage_appends(tmp_path):
    agent, params, clock, _, thread = fixture(tmp_path)
    settled(agent, params, clock)
    assert publish_model_metrics(agent, params, pending=False)["input_tokens"] == 1000
    background = SimpleNamespace(request_id="background", run_id="curator")
    decision(agent, background, usage={"input_tokens": 12})
    persist(agent, background, thread)
    refreshed = publish_model_metrics(agent, params, pending=False)
    assert refreshed["input_tokens"] == 1012 and refreshed["decision_input_tokens"] == 12
    assert publish_model_metrics(agent, params, pending=False)["input_tokens"] == 1012


def test_legacy_history_retains_unknown_partition(tmp_path):
    agent, params, _, _, thread = fixture(tmp_path)
    decision(agent, params, usage={"input_tokens": 12})
    legacy = deepcopy(model_call_summary(agent, request_id=params.request_id, run_id=params.run_id))
    legacy.pop("purpose_breakdown")
    agent.conversation_store.model_usage.append_once({"event_id": "legacy", "thread_id": thread.thread_id,
        "request_id": "old", "run_id": "old", "source": "old", "model_calls": legacy})
    persist(agent, params, thread)
    assert not agent.conversation_store.model_usage.summary(thread.thread_id)["purpose_totals_known"]
    metrics = publish_model_metrics(agent, params, pending=False)
    assert metrics["input_tokens"] == 24 and metrics["decision_input_tokens"] == 12
    assert not metrics["decision_input_complete"]


@pytest.mark.parametrize("mutation", ["unknown_schema", "nested", "regression"])
def test_purpose_corruption_cannot_silently_drop_history(tmp_path, mutation):
    agent, params, _, _, thread = fixture(tmp_path)
    decision(agent, params, usage={"input_tokens": 12})
    persist(agent, params, thread)
    summary = deepcopy(model_call_summary(agent, request_id=params.request_id, run_id=params.run_id))
    purpose = summary["purpose_breakdown"]
    if mutation == "unknown_schema":
        purpose["schema"] = "unknown"
    elif mutation == "nested":
        purpose["decision"]["purpose_breakdown"] = {}
    else:
        purpose["decision"]["usage_breakdown"]["provider"]["input_tokens"] = 0
    with pytest.raises(DataCorruptionError):
        persist(agent, params, thread, summary)


def test_optional_metrics_do_not_fabricate_zero_or_output():
    base = {"schema": "model_runtime_metrics.v1", "decision_call_count": 1}
    assert public_model_metrics(base)["decision_input_tokens"] is None
    assert "decision_call_count" not in public_model_metrics({"schema": base["schema"]})
