"""耗时来自原模型时钟；重放、迟到事件、明细裁剪和持久增量不丢失或重复累计。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.model_call_latency import model_call_latency_facts
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.model_metrics import (
    public_model_metrics,
    publish_model_metrics,
)


# LLM: 每次建立独立虚拟时钟及原 ModelCallLedger，没有模型 I/O；测试可推进时钟验证分区与差值。
# 函数用途: 返回用于比较原记录和累计快照的虚拟时钟环境。
def _ledger():
    clock = SimpleNamespace(now=10.0)
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=1), context=ModelCallLedgerContext(now=lambda: clock.now))
    return ledger, clock


# LLM: 使用原生命周期协议登记一次物理调用；purpose仅作为既有主/辅助/决策分桶事实，不代表运行授权。
# 函数用途: 给测试原账加入有准确请求身份的模型调用。
def _start(ledger, call_id, purpose="main"):
    ledger.started(ModelCallStartedParams(call_id, "fake", "fake", 10, request_id="request", run_id="run",
                                         metadata={"purpose": purpose, "auxiliary": purpose != "main"}))


def test_latency_accumulates_by_original_purpose_and_survives_evicted_details():
    ledger, clock = _ledger()
    _start(ledger, "main")
    clock.now += 2
    ledger.first_token(ModelCallFirstTokenParams("main"))
    clock.now += 5
    ledger.finished(ModelCallFinishParams("main", output_tokens=1))
    first = ledger.cumulative_summary(request_id="request", run_id="run")
    assert (first["model_latency_ms"], first["first_token_wait_ms"], first["post_first_token_ms"]) == (7000, 2000, 5000)
    _start(ledger, "decision", "decision")
    clock.now += 1
    ledger.timeout(ModelCallTimeoutParams("decision", 1, "wall_clock"))
    summary = ledger.cumulative_summary(request_id="request", run_id="run")
    assert len(ledger.records()) == 1
    assert summary["model_latency_ms"] == 8000
    assert summary["timed_model_call_count"] == 2
    assert summary["first_token_timed_call_count"] == summary["split_latency_call_count"] == 1
    assert summary["purpose_breakdown"]["main"]["model_latency_ms"] == 7000
    assert summary["purpose_breakdown"]["decision"]["model_latency_ms"] == 1000
    clock.now += 9
    ledger.provider_attempt(ModelCallProviderAttemptParams("decision", "late-http", "failed"))
    ledger.timeout(ModelCallTimeoutParams("decision", 10, "wall_clock"))
    late = ledger.cumulative_summary(request_id="request", run_id="run")
    assert late["model_latency_ms"] == 8000 and late["timed_model_call_count"] == 2


def test_stored_latency_uses_snapshot_delta_and_replay_keeps_original_total(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.threads.get_or_create({"canonical_user_id": "synthetic"})
    ledger, clock = _ledger()
    _start(ledger, "one")
    clock.now += 2
    ledger.first_token(ModelCallFirstTokenParams("one"))
    clock.now += 3
    ledger.finished(ModelCallFinishParams("one", output_tokens=1))
    request = {"thread_id": thread.thread_id, "request_id": "request", "run_id": "run",
               "model_calls": {"schema": "model_call_summary.v1", **ledger.cumulative_summary(request_id="request", run_id="run")}}
    store.model_usage.append_snapshot_once(request)
    _start(ledger, "two")
    clock.now += 1
    ledger.first_token(ModelCallFirstTokenParams("two"))
    clock.now += 2
    ledger.finished(ModelCallFinishParams("two", output_tokens=1))
    request["model_calls"] = {"schema": "model_call_summary.v1", **ledger.cumulative_summary(request_id="request", run_id="run")}
    store.model_usage.append_snapshot_once(request)
    store.model_usage.append_snapshot_once(request)
    events, errors = ConversationStore(tmp_path).model_usage.events_report(thread.thread_id)
    assert not errors and len(events) == 2
    assert sum(row.model_calls["model_latency_ms"] for row in events) == 8000
    assert sum(row.model_calls["first_token_wait_ms"] for row in events) == 3000
    assert sum(row.model_calls["post_first_token_ms"] for row in events) == 5000
    assert sum(row.model_calls["timed_model_call_count"] for row in events) == 2


def test_metrics_persist_request_latency_without_prompt_or_credential(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.threads.get_or_create({"canonical_user_id": "synthetic"})
    ledger, clock = _ledger()
    _start(ledger, "one", "auxiliary")
    clock.now += 4
    ledger.finished(ModelCallFinishParams("one", output_tokens=1))
    agent = SimpleNamespace(conversation_store=store, _model_call_ledger=ledger)
    params = SimpleNamespace(request_id="request", run_id="run", live_archive_state={},
                             task_attributes={"conversation_thread_id": thread.thread_id})
    metrics = publish_model_metrics(agent, params, pending=False)
    assert metrics["request_model_latency_ms"] == metrics["request_auxiliary_latency_ms"] == 4000
    assert metrics["request_timed_model_call_count"] == 1 and metrics["request_split_latency_call_count"] == 0
    assert publish_model_metrics(agent, params, pending=False)["request_model_latency_ms"] == 4000
    assert store.threads.require(thread.thread_id).model_metrics["request_model_latency_ms"] == 4000
    clean = public_model_metrics({**metrics, "prompt": "synthetic secret", "api_key": "synthetic secret"})
    assert "prompt" not in clean and "api_key" not in clean
    assert "request_model_latency_ms" not in public_model_metrics({"schema": "model_runtime_metrics.v1"})
    agent._model_call_ledger = SimpleNamespace(cumulative_summary=lambda **_: {})
    params.request_id = "another-request"
    params.live_archive_state = {}
    assert "request_model_latency_ms" not in publish_model_metrics(agent, params, pending=True)


@pytest.mark.parametrize("value", [None, True, -1, float("nan"), float("inf"), 1e308])
def test_unknown_or_invalid_clock_never_becomes_observed_zero(value):
    facts = model_call_latency_facts(SimpleNamespace(total_latency_seconds=value, first_token_latency_seconds=value))
    assert facts["timed_model_call_count"] == facts["first_token_timed_call_count"] == facts["split_latency_call_count"] == 0


def test_zero_latency_is_real_timed_observation():
    facts = model_call_latency_facts(SimpleNamespace(total_latency_seconds=0.0, first_token_latency_seconds=0.0))
    assert facts["timed_model_call_count"] == facts["first_token_timed_call_count"] == facts["split_latency_call_count"] == 1
