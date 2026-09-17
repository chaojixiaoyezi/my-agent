"""模型统计的确定性验证；真实模型与 TUI 验收单独记录。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    model_call_summary,
    record_model_call_finished,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
from agent_py_agent.agent.conversation.auxiliary_model_call import settle_standalone_model_usage
from agent_py_agent.agent.conversation.model_metrics import (
    model_metrics_from_thread,
    newer_model_metrics,
    public_model_metrics,
    publish_model_metrics,
)
from agent_py_agent.agent.conversation.runtime import _minimal_context_bundle
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.cli.chat_parts.tui_block_renderer import TuiRenderContext, _render_input_status
from agent_py_agent.cli.chat_parts.tui_markdown import display_width_text
from agent_py_agent.cli.chat_parts.tui_model_metrics import render_model_metrics
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnSummary


# LLM: 本夹具绑定一个独立 store、thread 和模型账本；不会发真实请求或改变宿主配置。
# 函数用途: 构造可验证主子代理归属及调用用量的本地环境。
def fixture(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.get_or_create_thread({"canonical_user_id": "test-user"})
    clock = SimpleNamespace(now=10.0)
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=2), context=ModelCallLedgerContext(now=lambda: clock.now))
    runtime = TuiRuntime("metrics-test")
    params = SimpleNamespace(request_id="request-1", run_id="run-1", live_archive_state={},
        task_attributes={"conversation_thread_id": thread.thread_id}, effective_on_chunk=runtime.begin_turn("request-1"))
    return SimpleNamespace(conversation_store=store, _model_call_ledger=ledger), params, clock, runtime, thread


# LLM: 只造结构化模型完成事实，缓存含在总输入中；不从正文推断任何指标。
# 函数用途: 添加一条可复用的模型调用样本。
def settled(agent, params, clock, *, call_id="call-1", usage=None, logical_id=""):
    ledger = agent._model_call_ledger
    ledger.started(ModelCallStartedParams(call_id, "fixture", "fixture-model", 1000,
        request_id=params.request_id, run_id=params.run_id, metadata={"logical_call_id": logical_id or call_id}))
    clock.now += 2
    ledger.first_token(ModelCallFirstTokenParams(call_id))
    clock.now += 5
    response = SimpleNamespace(text="完成", usage=usage if usage is not None else {
        "prompt_tokens": 1000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 750}})
    record_model_call_finished(ledger, call_id, response)
    return response


def test_round_tools_cache_speed_and_no_double_count(tmp_path):
    agent, params, clock, runtime, thread = fixture(tmp_path)
    response = settled(agent, params, clock)
    metrics = publish_model_metrics(agent, params, pending=False, tool_count=3, response=response, call_id="call-1")
    assert metrics["model_rounds"] == 1 and metrics["tool_count"] == 3
    assert metrics["input_tokens"] == 1000 and metrics["output_tokens"] == 100
    assert metrics["cache_percent"] == 75 and metrics["output_tps"] == 20
    replay = publish_model_metrics(agent, params, pending=False, tool_count=3)
    assert replay["input_tokens"] == 1000 and replay["cache_percent"] == 75
    assert runtime.store.snapshot().status.model_metrics == replay
    assert model_metrics_from_thread(agent.conversation_store, thread.thread_id) == replay


def test_cache_comparison_survives_store_reload_with_same_response_metrics(tmp_path):
    from agent_py_agent.agent.backends.cache_diagnostics import request_surface

    agent, params, clock, _runtime, thread = fixture(tmp_path)
    response = settled(agent, params, clock)
    agent._model_call_ledger.provider_attempt(ModelCallProviderAttemptParams(
        "call-1", "attempt", "finished", request_surface=request_surface({"messages": []}, "private endpoint")))
    metrics = publish_model_metrics(agent, params, pending=False, response=response, call_id="call-1")
    assert metrics["cache_percent"] == 75
    assert metrics["cache_diagnostic"]["baseline_available"] is False
    assert model_metrics_from_thread(ConversationStore(tmp_path), thread.thread_id) == metrics


@pytest.mark.parametrize("usage,expected", [
    ({"prompt_tokens": 100, "completion_tokens": 2}, None),
    ({"prompt_tokens": 100, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 0}}, 0),
    ({"input_tokens": 10, "cache_read_input_tokens": 70, "cache_creation_input_tokens": 20, "output_tokens": 2}, 70),
    ({"input_tokens": 100, "input_tokens_details": {"cached_tokens": 80}, "output_tokens": 2}, 80),
    ({}, None),
])
def test_cache_unknown_is_not_zero_and_protocols_share_denominator(tmp_path, usage, expected):
    agent, params, clock, _runtime, _thread = fixture(tmp_path)
    response = settled(agent, params, clock, usage=usage)
    metrics = publish_model_metrics(agent, params, pending=False, response=response)
    assert metrics["cache_percent"] == expected
    if not usage:
        assert metrics["estimated_tokens"] > 0 and metrics["unreported_calls"] == 1


def test_previous_requests_accumulate_but_current_persisted_snapshot_not_added_twice(tmp_path):
    agent, params, clock, _runtime, thread = fixture(tmp_path)
    settled(agent, params, clock)
    summary = model_call_summary(agent, request_id=params.request_id)
    for event_id, request_id in (("previous", "old-request"), ("current", params.request_id)):
        agent.conversation_store.append_model_usage_once({"event_id": event_id, "thread_id": thread.thread_id,
            "request_id": request_id, "run_id": params.run_id, "task_id": "task-1", "source": "test", "model_calls": summary})
    metrics = publish_model_metrics(agent, params, pending=False, tool_count=0)
    assert metrics["input_tokens"] == 2000 and metrics["output_tokens"] == 200
    assert metrics["model_rounds"] == 1
    assert publish_model_metrics(agent, params, pending=True)["input_tokens"] == 2000


def test_usage_handoff_settles_once_and_restart_adds_new_calls(tmp_path):
    from agent_py_agent.agent.agent_core._finalization_service import FinalizationService

    agent, params, clock, _runtime, thread = fixture(tmp_path)
    params.task_id, params.source = "task-1", "gateway"
    settled(agent, params, clock)
    finalizer = FinalizationService(agent)
    first = finalizer.settle_model_usage(params)
    params.source = "background_main_agent"
    assert finalizer.settle_model_usage(params) == first
    settled(agent, params, clock, call_id="second-call")
    finalizer.settle_model_usage(params)
    assert agent.conversation_store.model_usage_summary(thread.thread_id)["provider"]["input_tokens"] == 2000

    # 同一任务/消息在进程重启后是新的真实调用，不得与旧调用相减或复用事件键。
    agent._model_call_ledger = ModelCallLedger()
    params.live_archive_state = {}
    settled(agent, params, clock, call_id="new-process-call")
    value = publish_model_metrics(agent, params, pending=False)
    assert value["input_tokens"] == 3000
    finalizer.settle_model_usage(params)
    rows, errors = agent.conversation_store.model_usage_events_report(thread.thread_id)
    assert not errors and len(rows) == 3
    assert rows[0].model_calls["usage_scope_id"] != rows[-1].model_calls["usage_scope_id"]
    assert agent.conversation_store.model_usage_summary(thread.thread_id)["provider"]["input_tokens"] == 3000


@pytest.mark.parametrize("error", [InterruptedError("cancelled"), RuntimeError("provider failed")])
def test_exception_closeout_keeps_known_usage_without_masking_error(tmp_path, monkeypatch, error):
    from agent_py_agent.agent.agent_core import runtime_mixin
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams

    agent, sample, clock, _runtime, thread = fixture(tmp_path)
    agent.config = SimpleNamespace(enable_tools=False, auto_save_memory=False)
    params = RunParams(request_id=sample.request_id, run_id=sample.run_id, task_id="task-1",
        source="gateway", save=False, task_attributes=sample.task_attributes)

    def fail_after_a_real_settlement(*_args, **_kwargs):
        settled(agent, params, clock)
        raise error

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", fail_after_a_real_settlement)
    with pytest.raises(type(error)) as caught:
        runtime_mixin._run_once_with_params(agent, "继续整理原项目", params)
    assert caught.value is error
    summary = agent.conversation_store.model_usage_summary(thread.thread_id)
    assert summary["provider"]["input_tokens"] == 1000
    assert summary["provider"]["output_tokens"] == 100


def test_evicted_usage_scope_reentry_is_new_epoch(tmp_path):
    agent, params, clock, _runtime, _thread = fixture(tmp_path)
    settled(agent, params, clock)
    old = model_call_summary(agent, request_id=params.request_id)["usage_scope_id"]
    for index in range(8):
        params.request_id, params.run_id = f"other-{index}", f"run-other-{index}"
        settled(agent, params, clock, call_id=f"other-call-{index}")
    params.request_id, params.run_id = "request-1", "run-1"
    settled(agent, params, clock, call_id="reentered")
    fresh = model_call_summary(agent, request_id=params.request_id)
    assert fresh["usage_scope_id"] != old
    assert fresh["physical_model_attempt_count"] == 1


def test_pruned_detail_preserves_total_and_retry_does_not_add_logical_round(tmp_path):
    agent, params, clock, _runtime, _thread = fixture(tmp_path)
    for i in range(6):
        settled(agent, params, clock, call_id=f"call-{i}", logical_id=f"logical-{i // 2}")
    metrics = publish_model_metrics(agent, params, pending=False, tool_count=1)
    assert metrics["model_rounds"] == 3 and metrics["retry_count"] == 3
    assert metrics["input_tokens"] == 6000 and len(agent._model_call_ledger.records()) == 2


def test_child_projection_isolation_and_no_model_context_change(tmp_path):
    agent, params, clock, runtime, main = fixture(tmp_path)
    child = agent.conversation_store.get_or_create_thread({"canonical_user_id": "test-child", "channel_conversation_id": "child"})
    params.task_attributes["agent_thread_id"] = child.thread_id
    before = agent.conversation_store.context_bundle(child.thread_id)
    response = settled(agent, params, clock)
    publish_model_metrics(agent, params, pending=False, response=response)
    assert agent.conversation_store.load_thread(main.thread_id).model_metrics == {}
    stored = agent.conversation_store.load_thread(child.thread_id)
    assert stored.model_metrics and stored.updated_at == child.updated_at
    assert agent.conversation_store.context_bundle(child.thread_id) == before
    assert _minimal_context_bundle(child) == _minimal_context_bundle(stored)
    reopened = ConversationStore(tmp_path)
    frame = conversation_agent_activity(SimpleNamespace(), reopened, child.thread_id).to_dict()
    other = TuiRuntime("child-test")
    other.update_background_activity(0, frame)
    assert other.store.snapshot().status.model_metrics == runtime.store.snapshot().status.model_metrics


def test_isolated_call_does_not_replace_visible_task_metrics(tmp_path):
    agent, params, clock, runtime, _thread = fixture(tmp_path)
    response = settled(agent, params, clock)
    publish_model_metrics(agent, params, pending=False, response=response)
    before = runtime.store.snapshot()
    params.context_scope = "isolated"
    assert publish_model_metrics(agent, params, pending=True) == {}
    assert runtime.store.snapshot() == before


def test_compact_idle_and_older_poll_do_not_clear_metrics():
    runtime = TuiRuntime("retained")
    value = {"schema": "model_runtime_metrics.v1", "sampled_at_ns": 200, "input_tokens": 1200, "model_rounds": 4, "totals_known": True}
    turn = runtime.begin_turn("r")
    turn.write_model_metrics(value)
    runtime.update_background_activity(0, {"compact_count": 1, "model_metrics": {**value, "sampled_at_ns": 100, "input_tokens": 100}})
    runtime.complete_turn("r", TuiTurnSummary(response_text="结束"))
    assert runtime.store.snapshot().status.model_metrics["input_tokens"] == 1200
    assert runtime.store.snapshot().status.phase == "idle"


def test_standalone_compact_usage_is_durable_idempotent_and_visible_next_turn(tmp_path):
    agent, params, clock, _runtime, thread = fixture(tmp_path)
    settled(agent, params, clock)
    agent.conversation_store.append_model_usage_once({"event_id": "previous", "thread_id": thread.thread_id,
        "request_id": params.request_id, "run_id": params.run_id, "task_id": "task-1", "source": "test",
        "model_calls": model_call_summary(agent, request_id=params.request_id)})
    params.request_id = "compact-operation-1"
    settled(agent, params, clock, call_id="compact-call", usage={"input_tokens": 400, "output_tokens": 40})
    request = SimpleNamespace(agent=agent, store=agent.conversation_store, thread=thread,
        request_id=params.request_id, run_id=params.run_id, task_id="task-1", operation_id="compact-1")
    settle_standalone_model_usage(request)
    settle_standalone_model_usage(request)
    metrics = model_metrics_from_thread(agent.conversation_store, thread.thread_id)
    assert metrics["input_tokens"] == 1400 and metrics["output_tokens"] == 140
    assert metrics["model_rounds"] == 1 and metrics["tool_count"] == 0
    rows, errors = agent.conversation_store.model_usage_events_report(thread.thread_id)
    assert not errors and len(rows) == 2
    params.request_id = "after-compact"
    params.live_archive_state = {}
    settled(agent, params, clock, call_id="next-call", usage={"input_tokens": 100, "output_tokens": 10})
    value = publish_model_metrics(agent, params, pending=False)
    assert value["input_tokens"] == 1500 and value["output_tokens"] == 150


def test_standalone_compact_failure_keeps_explicit_unknown_usage(tmp_path):
    from agent_py_agent.agent.agent_core.model.call_runtime import record_model_call_failed

    agent, params, _clock, _runtime, thread = fixture(tmp_path)
    ledger = agent._model_call_ledger
    ledger.started(ModelCallStartedParams("failed-compact", "fixture", "fixture-model", 100,
        request_id=params.request_id, run_id=params.run_id))
    record_model_call_failed(ledger, "failed-compact", RuntimeError("provider failed"))
    request = SimpleNamespace(agent=agent, store=agent.conversation_store, thread=thread,
        request_id=params.request_id, run_id=params.run_id, task_id="task-1", operation_id="failed-compact")
    settle_standalone_model_usage(request)
    metrics = model_metrics_from_thread(agent.conversation_store, thread.thread_id)
    assert metrics["unreported_calls"] >= 1


@pytest.mark.parametrize("standalone,failed", [(True, False), (True, True), (False, False), (False, True)])
def test_compact_settles_own_scope_on_success_and_failure(tmp_path, monkeypatch, standalone, failed):
    from agent_py_agent.agent.conversation import compact

    agent, params, clock, _runtime, thread = fixture(tmp_path)
    request = SimpleNamespace(agent=agent, store=agent.conversation_store, thread=thread,
        request_id=params.request_id, run_id=params.run_id, task_id="task-1", operation_id="compact-op",
        standalone_usage=standalone)

    # LLM: 模拟候选模型已计入内存账，后续提交可能失败；不产生文件业务产物或真实请求。
    # 函数用途: 验证调用链无论摘要能否提交，都只结算独立范围，不与普通 finalizer 重复计费。
    def candidate(_request):
        settled(agent, params, clock)
        if failed:
            raise RuntimeError("candidate cannot commit")
        return SimpleNamespace(projected_tokens=200)

    monkeypatch.setattr(compact, "_compact_pending", candidate)
    monkeypatch.setattr(compact, "_emit_compact_progress", lambda *a, **kw: None)
    if failed:
        with pytest.raises(RuntimeError, match="candidate cannot commit"):
            compact._execute_compact_request(request)
    else:
        compact._execute_compact_request(request)
    events, errors = agent.conversation_store.model_usage_events_report(thread.thread_id)
    assert not errors and len(events) == int(standalone)


@pytest.mark.parametrize("width", [1, 20, 60, 90, 120, 200])
def test_renderer_bounded_and_separate_from_context(width):
    metrics = public_model_metrics({"schema": "model_runtime_metrics.v1", "model_rounds": 8, "tool_count": 2,
        "input_tokens": 1_300_000, "output_tokens": 1000, "cache_percent": None, "totals_known": True})
    lines = render_model_metrics(metrics, width)
    assert len(lines) == 1
    text = "".join(fragment[1] for fragment in lines[0])
    assert display_width_text(text) <= width
    if width >= 90:
        assert "当轮工具 2" in text and "最近缓存 —" in text and "会话累计 1.3m" in text
    runtime = TuiRuntime("render")
    rendered = _render_input_status(runtime.store.snapshot(), TuiRenderContext(width=width, model_metrics=tuple(metrics.items())))
    assert rendered == lines


def test_metrics_whitelist_preserves_unknown_and_orders_samples():
    assert public_model_metrics({"schema": "unknown"}) == {}
    result = public_model_metrics({"schema": "model_runtime_metrics.v1", "tool_count": True,
        "cache_percent": float("inf"), "output_tps": -1, "input_tokens": {}, "private": "credential"})
    assert result["tool_count"] is None and result["cache_percent"] is None and result["output_tps"] is None
    assert "credential" not in str(result)
    assert newer_model_metrics({**result, "sampled_at_ns": 300}, {**result, "sampled_at_ns": 200})["sampled_at_ns"] == 300


def test_gateway_rich_event_reaches_same_tui_projection(tmp_path):
    import json

    from agent_py_agent.agent.gateway_parts.request_execution import BufferedChunkStreamWriter

    path = tmp_path / "chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)
    data = {"schema": "model_runtime_metrics.v1", "model_rounds": 2, "tool_count": 4, "input_tokens": 2300, "sampled_at_ns": 10, "api_key": "never-copy"}
    assert writer.write_model_metrics(data)
    writer.close()
    runtime = TuiRuntime("gateway-metrics")
    turn = runtime.begin_turn("request")
    row = json.loads(path.read_text().split("\n")[0])
    assert row["kind"] == "model_metrics_updated"
    assert turn.on_gateway_event(row)
    assert runtime.store.snapshot().status.model_metrics["tool_count"] == 4
    assert "never-copy" not in path.read_text()
    assert not any(b.role == "assistant" for b in runtime.store.snapshot().active_blocks)


def test_broken_telemetry_cannot_fail_model_call(tmp_path, monkeypatch):
    agent, params, clock, _runtime, _thread = fixture(tmp_path)
    settled(agent, params, clock)

    # LLM: 注入显示存储失败，不接管任何模型/任务终态；断言遥测隔离合同。
    # 函数用途: 模拟故障，确保业务调用不会随统计条更新失败而被打断。
    def broken(*_args, **_kwargs):
        raise KeyError("unavailable thread")

    monkeypatch.setattr(agent.conversation_store, "update_model_metrics", broken)
    assert publish_model_metrics(agent, params, pending=False) == {}
    assert agent._model_call_ledger.records()[-1].status == "finished"
