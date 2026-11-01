"""使用原记账/Goal/成本/持久统计入口核对归一；合成usage不签发线上缓存收益。"""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import record_model_call_finished
from agent_py_agent.agent.agent_core.model.llm_metrics import usage_cost_usd
from agent_py_agent.agent.agent_core.model.usage import goal_token_usage
from agent_py_agent.agent.agent_core.runtime.goal_accounting import account_goal_model_response
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.model_metrics import (
    _last_response_metrics,
    publish_model_metrics,
)
from agent_py_agent.agent.llm_scale import model_pricing
from agent_py_agent.tests.test_conversation_goal_tools import _goal_agent
from scripts.analyze_cache_ledger import analyze

CASES = [
    ({"input_tokens": 10000, "output_tokens": 100,
      "input_tokens_details": {"cached_tokens": 9000, "cache_write_tokens": 1000}}, 9000, 1000, 90.0),
    ({"prompt_tokens": 10000, "completion_tokens": 100,
      "prompt_cache_hit_tokens": 9000, "prompt_cache_miss_tokens": 1000}, 9000, None, 90.0),
    ({"input_tokens": 0, "output_tokens": 100,
      "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 1000}, 9000, 1000, 90.0),
    ({"prompt_tokens": 10000, "completion_tokens": 100,
      "cache_read_input_tokens": 0, "prompt_cache_hit_tokens": 9000}, 0, None, 0.0),
    ({"prompt_tokens": 10000, "completion_tokens": 100}, None, None, None),
]


def settle(ledger, now, thread_id, call_id, usage, purpose="main"):
    ledger.started(ModelCallStartedParams(call_id, "synthetic", "fixture-model", 777,
        request_id="request", run_id="run", metadata={"purpose": purpose,
        "auxiliary": purpose != "main", "thread_id": thread_id, "logical_call_id": call_id}))
    now[0] += 1
    response = ModelResponse(text="fixture", backend="synthetic", usage=usage)
    record_model_call_finished(ledger, call_id, response)
    return response


@pytest.mark.parametrize("usage,read,write,percent", CASES)
def test_original_consumers_and_offline_report_agree_without_cross_domain_average(
    tmp_path, monkeypatch, usage, read, write, percent,
):
    agent, thread, goal = _goal_agent(tmp_path)
    now = [10.0]
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: now[0]))
    agent._model_call_ledger = ledger
    original = deepcopy(usage)
    response = settle(ledger, now, thread.thread_id, "main", usage)
    expected_goal = 10100 - (read or 0)
    agent.conversation_store.goals.update({"thread_id": thread.thread_id,
        "goal_id": goal.goal_id, "token_budget": expected_goal + 1})
    params = SimpleNamespace(task_id=goal.task_id, request_id="request", run_id="run",
        task_attributes=agent._current_run_params.task_attributes, live_archive_state={}, runtime_injections=[])
    accounted = account_goal_model_response(agent, params, response)
    assert accounted is not None and accounted.tokens_used == expected_goal
    assert accounted.status == "active" and not params.runtime_injections
    assert goal_token_usage(response) == expected_goal

    # 单价由测试配置提供；走真实分档算法，不冒充真实提供方报价。
    monkeypatch.setattr(model_pricing, "_OVERRIDES", {
        "synthetic-price": model_pricing.ModelPrice(1, 0, 0.1, 1.25)})
    if write == 1000:
        assert usage_cost_usd("synthetic-price", response) == (0.00215, False)
    else:
        cost, partial = usage_cost_usd("synthetic-price", response)
        assert cost == pytest.approx((10000 - (read or 0) + (read or 0) * 0.1) / 1_000_000)
        assert partial is False

    settle(ledger, now, thread.thread_id, "auxiliary",
        {"prompt_tokens": 20000, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 1000}}, "compact")
    decision = settle(ledger, now, thread.thread_id, "decision",
        {"prompt_tokens": 50000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 49000}}, "decision")
    assert _last_response_metrics(decision, ledger, "decision") == {}
    metrics = publish_model_metrics(agent, params, pending=False, response=response, call_id="main")
    assert metrics["cache_percent"] == percent, "末次主请求不能换成全ledger均值"
    assert metrics["input_tokens"] == 30000, "统计保留辅助用量，但决策分区必须剔除"
    assert metrics["output_tokens"] == 120
    assert metrics["cache_read_input_tokens"] == (read or 0) + 1000
    assert metrics["decision_input_tokens"] == 50000

    summary = ledger.cumulative_summary(request_id="request")
    assert summary["logical_model_turn_count"] == summary["physical_model_attempt_count"] == 3
    assert summary["accounted_input_tokens"] == 80000 and summary["output_tokens"] == 170
    main = summary["purpose_breakdown"]["main"]["usage_breakdown"]["provider"]
    assert main["input_tokens"] == 10000 and main["output_tokens"] == 100
    assert main["cache_read_input_tokens"] == (read or 0)
    assert main["cache_write_input_tokens"] == (write or 0)
    assert main["cache_read_input_tokens_reported_call_count"] == int(read is not None)
    assert main["cache_write_input_tokens_reported_call_count"] == int(write is not None)

    store = agent.conversation_store
    snapshot = {"thread_id": thread.thread_id, "request_id": "request", "run_id": "run",
        "model_calls": {"schema": "model_call_summary.v1", **summary}}
    store.model_usage.append_snapshot_once(snapshot)
    store.model_usage.append_snapshot_once(snapshot)
    reloaded = ConversationStore(store.storage.root)
    events, errors = reloaded.model_usage.events_report(thread.thread_id)
    assert not errors and len(events) == 1
    assert events[0].model_calls["purpose_breakdown"]["main"]["usage_breakdown"]["provider"] == main

    rows = [{"call_id": record.call_id, "domain": record.model, "segment": "synthetic",
             "purpose": record.metadata["purpose"], "source": "synthetic_provider_fields",
             "sequence": index, "prompt_tokens": record.accounted_input_tokens,
             "cached_tokens": record.cached_input_tokens if "cache_read_input_tokens" in record.provider_usage_fields else None,
             "prompt": "PRIVATE-FIXTURE-NOT-OUTPUT"}
            for index, record in enumerate(ledger.records(), 1)]
    report = analyze(rows, [])
    assert report["coverage"]["request_count"] == 3
    assert report["coverage"]["counter_pair_request_coverage"] == (1 if read is not None else 2 / 3)
    assert report["coverage"]["paired_prompt_tokens"] == (80000 if read is not None else 70000)
    assert len(report["groups"]) == 3
    assert report["segments"][0]["conditional_uncached_floor_tokens"] is None
    assert "PRIVATE-FIXTURE" not in json.dumps(report)
    assert usage == original
