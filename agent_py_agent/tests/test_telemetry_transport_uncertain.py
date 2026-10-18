# LLM: 真实临时会话及原遥测公开入口只把普通故障降级；原执行未知必须保持同一 typed 对象，不能按文案或 code 属性冒认。
# 模块用途: 覆盖读、写和统计发布中的未知传播，保留采样绑定和普通显示故障的既有语义。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import auxiliary_model_call, context_usage, model_metrics
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.tests.test_conversation_context_usage import usage

_STAGES = (
    "capture", "context-write", "metrics-thread", "metrics-events", "metrics-write",
    "metrics-sink", "metrics-summary", "metrics-restore", "auxiliary-append", "auxiliary-publish",
)


# LLM: 相同错误编号不构成原执行未知类型；此替身只验证自然文案和属性不能改变结算。
# 类用途: 防止用 error_code 或字符串替代异常类型核验。
class _OrdinaryDisplayFailure(RuntimeError):
    error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"


# LLM: 所有持久状态只在 pytest 临时 home；无 Agent、模型、线程或服务启动。
# 函数用途: 准备原 Store、线程、真实模型账本与一次遥测参数。
def _case(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    store = ConversationStore(tmp_path / "home" / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-a"})
    agent = SimpleNamespace(conversation_store=store, _model_call_ledger=ModelCallLedger())
    params = SimpleNamespace(
        request_id="request-a", run_id="run-a", context_scope="conversation",
        task_attributes={"conversation_thread_id": thread.thread_id}, live_archive_state={},
        effective_on_chunk=SimpleNamespace(write_model_metrics=lambda _payload: None),
    )
    binding = context_usage.capture_context_usage_binding(agent, params)
    assert isinstance(binding, context_usage.ContextUsageBinding)
    return agent, params, thread, binding


# LLM: 仅在实际领域 I/O 边界注入一次失败，公开遥测链和原 CAS 不被替换。
# 函数用途: 让原读写方法或显示 sink 返回指定异常，记录实际到达次数。
def _inject(agent, params, stage, error, monkeypatch):
    store = agent.conversation_store
    seen = []
    if stage.startswith("auxiliary-"):
        agent._model_call_ledger.started(ModelCallStartedParams(
            "synthetic-auxiliary", "offline", "offline-model", 32,
            request_id=params.request_id, run_id=params.run_id,
        ))

    def fail(*_args, **_kwargs):
        seen.append(error)
        raise error

    targets = {
        "capture": (store.threads, "load_report"),
        "context-write": (store.model_usage, "update_context_usage"),
        "metrics-thread": (store.threads, "load_report"),
        "metrics-events": (store.model_usage, "events_report"),
        "metrics-write": (store.model_usage, "update_metrics"),
        "metrics-sink": (params.effective_on_chunk, "write_model_metrics"),
        "metrics-summary": (agent._model_call_ledger, "cumulative_summary"),
        "metrics-restore": (store.threads, "load_report"),
        "auxiliary-append": (store.model_usage, "append_snapshot_once"),
        "auxiliary-publish": (store.model_usage, "update_metrics"),
    }
    target, name = targets[stage]
    monkeypatch.setattr(target, name, fail)
    return seen


# LLM: 测试只走公开入口；保存使用同次原绑定，统计调用不替代执行或结算。
# 函数用途: 调用所选遥测链，让失败必须穿过真实 best-effort catch。
def _invoke(agent, params, thread, binding, stage):
    if stage == "capture":
        return context_usage.capture_context_usage_binding(agent, params)
    if stage == "context-write":
        return context_usage.record_model_context_usage(agent, params, usage(), binding=binding)
    if stage == "metrics-restore":
        return model_metrics.model_metrics_from_thread(agent.conversation_store, thread.thread_id)
    if stage.startswith("auxiliary-"):
        request = SimpleNamespace(
            agent=agent, store=agent.conversation_store, thread=thread,
            request_id=params.request_id, run_id=params.run_id, task_id="synthetic-task",
        )
        return auxiliary_model_call.settle_standalone_model_usage(request)
    return model_metrics.publish_model_metrics(agent, params, pending=False, tool_count=0)


@pytest.mark.parametrize("stage", _STAGES)
def test_original_transport_uncertain_is_not_swallowed_by_telemetry(tmp_path, monkeypatch, stage):
    agent, params, thread, binding = _case(tmp_path, monkeypatch)
    original = RequestExecutionOutcomeUncertainError({
        "id": params.request_id, "execution_attempt_id": "attempt-a", "lease_epoch": 7,
        "lease_owner": "lease-owner-a",
    }, "telemetry-test")
    seen = _inject(agent, params, stage, original, monkeypatch)
    records_before = agent._model_call_ledger.records()
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        _invoke(agent, params, thread, binding, stage)
    assert caught.value is original
    assert seen == [original]
    assert (original.request_id, original.execution_attempt_id, original.lease_epoch,
            original.lease_owner, original.operation_entered, original.phase) == (
        "request-a", "attempt-a", 7, "lease-owner-a", False, "telemetry-test",
    )
    assert agent._model_call_ledger.records() == records_before
    if stage == "auxiliary-publish":
        events, errors = agent.conversation_store.model_usage.events_report(thread.thread_id)
        assert not errors and len(events) == 1


@pytest.mark.parametrize("stage", _STAGES)
@pytest.mark.parametrize("failure", [OSError, RuntimeError, _OrdinaryDisplayFailure])
def test_ordinary_display_failures_remain_best_effort(tmp_path, monkeypatch, stage, failure):
    agent, params, thread, binding = _case(tmp_path, monkeypatch)
    original = failure("ACTIVE_TURN_OUTCOME_UNCERTAIN ordinary diagnostic")
    seen = _inject(agent, params, stage, original, monkeypatch)
    records_before = agent._model_call_ledger.records()
    result = _invoke(agent, params, thread, binding, stage)
    assert seen == [original]
    assert result is None or result is False or isinstance(result, dict)
    assert agent._model_call_ledger.records() == records_before
