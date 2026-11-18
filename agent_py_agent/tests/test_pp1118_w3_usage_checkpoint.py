"""W3（pro-pack-20261118）：逐次调用幂等落账检查点与基线版本号 CAS 回归测试。"""
from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import FinalizationService
from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext, ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_model_generation import (
    ModelGenerateParams,
    generate_model_response,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallLedgerContext,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.model_metrics import publish_model_metrics
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_f7_usage_accounting_contract import _settle


class _UsageFakeBackend:
    """只返回可控 usage 的本地假后端，不发起任何网络请求。"""

    name = "fake"
    model_name = "same-model"

    def __init__(self, responses: list[tuple[int, int, int | None]], *, on_generate=None) -> None:
        self._responses = list(responses)
        self.calls = 0
        self._on_generate = on_generate

    def generate(self, prompt: str, on_chunk=None, **kwargs: object) -> ModelResponse:
        del prompt, on_chunk, kwargs
        if callable(self._on_generate):
            self._on_generate(self.calls)
        prompt_tokens, completion_tokens, cached = self._responses[self.calls]
        self.calls += 1
        usage: dict[str, object] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        if cached is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
        return ModelResponse(text=f"reply-{self.calls}", backend=self.name, usage=usage)


def _loop_params(thread_id: str, *, request_id: str = "req-1", run_id: str = "run-1") -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt="hello",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={"conversation_thread_id": thread_id},
        request_id=request_id,
        run_id=run_id,
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=run_id, source_protocol="text"),
        save=False,
        source="gateway",
    )


def _make_agent(tmp_path, store: ConversationStore, backend: object, clock: SimpleNamespace) -> SimpleNamespace:
    home_dir = tmp_path / "home"
    return SimpleNamespace(
        backend=backend,
        conversation_store=store,
        _model_call_ledger=ModelCallLedger(context=ModelCallLedgerContext(now=lambda: clock.now)),
        config=SimpleNamespace(
            request_timeout=10,
            agent_name="test-agent",
            memory_archive_level=3,
            enable_self_learning=False,
        ),
        session_id="session-1",
        root=tmp_path / "workspace",
        home_paths=SimpleNamespace(owner_home_dir=home_dir, owner_id="synthetic-owner"),
        _current_subagent_run_id="",
    )


def _finalize_ctx(params: ToolLoopExecuteParams, final_response: ModelResponse) -> FinalizeContext:
    return FinalizeContext(
        user_prompt=params.user_prompt,
        final_prompt="hello",
        final_response=final_response,
        memories=[],
        executed_tools=[],
        archive_tool_calls=[],
        routed_context=SimpleNamespace(matches=[], required_read_paths=[], candidate_paths=[]),
        resume_context_result=None,
        runtime_injections=[],
        compression_snapshot_id="",
        compression_snapshot_path="",
        compression_applied=False,
        request_id=params.request_id,
        run_id=params.run_id,
        task_id=params.task_id,
        source=params.source,
        do_save=False,
        task_attributes=params.task_attributes,
        recovery_task_refs=None,
        recovery_content_paths=None,
        recovery_next_actions=None,
        tool_rounds=1,
    )


def test_per_call_checkpoints_survive_restart_and_finalize_sums_scopes_exactly(tmp_path) -> None:
    store = ConversationStore(tmp_path / "home" / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "synthetic-owner"})
    clock = SimpleNamespace(now=10.0)
    # Scope A：3 次调用（每次经真实 generate_model_response -> _finish_model_generation 自动检查点落账）
    backend_a = _UsageFakeBackend([(100, 10, 60), (200, 20, 120), (300, 30, 180)])
    agent = _make_agent(tmp_path, store, backend_a, clock)
    params_a = _loop_params(thread.thread_id)
    for round_idx in range(3):
        generate_model_response(
            ModelGenerateParams(agent=agent, params=params_a, prompt=f"turn-{round_idx}", tool_rounds=round_idx)
        )

    # 进程在 finalize 前中断也已持久保存 scope A 全部 3 次调用
    mid_summary = ConversationStore(store.storage.root).model_usage.summary(thread.thread_id)
    assert mid_summary["provider"]["input_tokens"] == 600
    assert mid_summary["provider"]["cache_read_input_tokens"] == 360
    assert mid_summary["provider"]["call_count"] == 3

    # 模拟重启：新账本（scope B），再完成 2 次调用并执行完整 finalize
    backend_b = _UsageFakeBackend([(400, 40, 240), (500, 50, 300)])
    agent.backend = backend_b
    agent._model_call_ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: clock.now))
    params_b = _loop_params(thread.thread_id)
    last_response: ModelResponse | None = None
    for round_idx in range(2):
        last_response = generate_model_response(
            ModelGenerateParams(agent=agent, params=params_b, prompt=f"resumed-{round_idx}", tool_rounds=round_idx)
        )
    assert last_response is not None
    FinalizationService(agent).finalize(_finalize_ctx(params_b, last_response))

    reopened = ConversationStore(store.storage.root)
    final_summary = reopened.model_usage.summary(thread.thread_id)
    assert final_summary["provider"]["input_tokens"] == 1500
    assert final_summary["provider"]["output_tokens"] == 150
    assert final_summary["provider"]["cache_read_input_tokens"] == 900
    assert final_summary["provider"]["call_count"] == 5
    events, errors = reopened.model_usage.events_report(thread.thread_id)
    assert not errors
    scope_ids = {event.model_calls.get("usage_scope_id") for event in events}
    assert len(scope_ids) == 2


def test_repeated_checkpoints_without_new_calls_are_idempotent(tmp_path) -> None:
    store = ConversationStore(tmp_path / "home" / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "synthetic-owner"})
    clock = SimpleNamespace(now=10.0)
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: clock.now))
    agent = _make_agent(tmp_path, store, _UsageFakeBackend([]), clock)
    agent._model_call_ledger = ledger

    params = _loop_params(thread.thread_id, request_id="request", run_id="run")
    service = FinalizationService(agent)
    assert service.checkpoint_model_usage(params) == ""
    assert service.checkpoint_model_usage(replace(params, task_attributes={})) == ""

    _settle(ledger, clock, thread.thread_id, call_id="call-1", purpose="main", seconds=2, cached=50)
    first_id = service.checkpoint_model_usage(params)
    second_id = service.checkpoint_model_usage(params)
    assert first_id and second_id == first_id

    events, errors = ConversationStore(store.storage.root).model_usage.events_report(thread.thread_id)
    assert not errors and len(events) == 1


def test_checkpoint_cas_avoids_events_reread_until_foreign_append(tmp_path, monkeypatch) -> None:
    store = ConversationStore(tmp_path / "home" / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "synthetic-owner"})
    clock = SimpleNamespace(now=10.0)
    backend = _UsageFakeBackend([(100, 10, 50), (100, 10, 60), (100, 10, 70)])
    agent = _make_agent(tmp_path, store, backend, clock)
    params = _loop_params(thread.thread_id, request_id="req-main", run_id="run-main")

    # 预置一次外部历史事件，首轮 publish 会全读一次建立 baseline 缓存
    store.model_usage.append_snapshot_once(
        {
            "thread_id": thread.thread_id,
            "request_id": "prior-req",
            "run_id": "prior-run",
            "task_id": "task-0",
            "source": "gateway",
            "model_calls": {
                "schema": "model_call_summary.v1",
                "usage_scope_id": "prior-scope",
                "physical_model_attempt_count": 1,
                "usage_breakdown": {
                    "schema": "model_usage_breakdown.v1",
                    "provider": {"input_tokens": 50, "output_tokens": 5, "cache_read_input_tokens": 25, "call_count": 1},
                    "estimated": {},
                },
            },
            "now": 1.0,
        }
    )

    read_calls = 0
    original_events_report = store.model_usage.events_report

    def counted_events_report(*args, **kwargs):
        nonlocal read_calls
        read_calls += 1
        return original_events_report(*args, **kwargs)

    monkeypatch.setattr(store.model_usage, "events_report", counted_events_report)

    # 第 1 次模型生成：start_model_call_record 首次读历史建缓存（read_calls == 1），
    # 随后 finish 内的 checkpoint_model_usage CAS 顺延 revision，publish_model_metrics 不再重读。
    generate_model_response(ModelGenerateParams(agent=agent, params=params, prompt="round-0", tool_rounds=0))
    assert read_calls == 1

    # 后续多轮模型生成（start pending + finish checkpoint + finish publish）零全量重读
    read_calls = 0
    generate_model_response(ModelGenerateParams(agent=agent, params=params, prompt="round-1", tool_rounds=1))
    assert read_calls == 0

    # 场景 A：检查点之后、publish_model_metrics 之前插入外部事件 -> 触发恰好 1 次重读
    store.model_usage.append_snapshot_once(
        {
            "thread_id": thread.thread_id,
            "request_id": "foreign-req-1",
            "run_id": "foreign-run-1",
            "task_id": "task-f1",
            "source": "compact",
            "model_calls": {
                "schema": "model_call_summary.v1",
                "usage_scope_id": "foreign-scope-1",
                "physical_model_attempt_count": 1,
                "usage_breakdown": {
                    "schema": "model_usage_breakdown.v1",
                    "provider": {"input_tokens": 40, "output_tokens": 4, "cache_read_input_tokens": 20, "call_count": 1},
                    "estimated": {},
                },
            },
            "now": 2.0,
        }
    )
    metrics_after_foreign = publish_model_metrics(agent, params, pending=False)
    assert read_calls == 1
    assert metrics_after_foreign["input_tokens"] == 290

    # 场景 B：in-flight 期间（start_model_call_record 发布之后、checkpoint 之前）插入外部事件 -> before != 缓存版本，CAS 拒绝顺延，finish 阶段 publish 重读 1 次
    read_calls = 0

    def inject_inflight_foreign(call_idx: int) -> None:
        if call_idx != 2:
            return
        store.model_usage.append_snapshot_once(
            {
                "thread_id": thread.thread_id,
                "request_id": "foreign-req-2",
                "run_id": "foreign-run-2",
                "task_id": "task-f2",
                "source": "compact",
                "model_calls": {
                    "schema": "model_call_summary.v1",
                    "usage_scope_id": "foreign-scope-2",
                    "physical_model_attempt_count": 1,
                    "usage_breakdown": {
                        "schema": "model_usage_breakdown.v1",
                        "provider": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 5, "call_count": 1},
                        "estimated": {},
                    },
                },
                "now": 3.0,
            }
        )

    backend._on_generate = inject_inflight_foreign
    generate_model_response(ModelGenerateParams(agent=agent, params=params, prompt="round-2", tool_rounds=2))
    assert read_calls == 1
    metrics_final = publish_model_metrics(agent, params, pending=False)
    assert read_calls == 1
    assert metrics_final["input_tokens"] == 400


def test_checkpoint_failure_does_not_break_model_call_and_reraises_transport_uncertain(
    tmp_path, monkeypatch, caplog
) -> None:
    store = ConversationStore(tmp_path / "home" / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "synthetic-owner"})
    clock = SimpleNamespace(now=10.0)
    backend = _UsageFakeBackend([(100, 10, 80)])
    agent = _make_agent(tmp_path, store, backend, clock)
    params = _loop_params(thread.thread_id)

    def broken_append(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store.model_usage, "append_snapshot_once", broken_append)
    with caplog.at_level(logging.WARNING):
        response = generate_model_response(
            ModelGenerateParams(agent=agent, params=params, prompt="hello", tool_rounds=0)
        )
    assert response.text == "reply-1"
    assert any("MODEL_USAGE_CHECKPOINT_FAILED error_type=OSError" in rec.getMessage() for rec in caplog.records)

    uncertain = RequestExecutionOutcomeUncertainError(
        {
            "id": "req-u",
            "execution_attempt_id": "att-u",
            "lease_epoch": 1,
            "lease_owner": "owner-u",
        },
        "usage_checkpoint",
    )

    def uncertain_append(*_args, **_kwargs):
        raise uncertain

    monkeypatch.setattr(store.model_usage, "append_snapshot_once", uncertain_append)
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        FinalizationService(agent).checkpoint_model_usage(params)
    assert caught.value is uncertain
