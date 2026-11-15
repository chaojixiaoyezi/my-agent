"""验证 nw3-w3：边界检查点、cache_control 迁移计数过滤与 Responses namespace 工具计数。"""

from __future__ import annotations

import copy
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.agent_core.tool_model_generation import (
    _invoke_backend_generate,
    _ModelGenerationState,
)
from agent_py_agent.agent.agent_core.tool_stream import ToolBoundaryChunkFilter
from agent_py_agent.agent.backends import gateway_helpers
from agent_py_agent.agent.backends import responses_websocket as ws
from agent_py_agent.agent.backends.anthropic_prompt_cache import (
    anthropic_messages_with_optional_cache,
)
from agent_py_agent.agent.backends.cache_diagnostics import (
    _CHAIN_CHECKPOINT_LIMIT_COUNT,
    compare_request_surfaces,
    request_surface,
)
from agent_py_agent.agent.backends.gateway_helpers import (
    GatewayRequest,
    current_observer_previous_message_count,
    provider_attempt_observer,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation.model_metrics import (
    _add_cache_change_counts,
    model_metrics_from_thread,
    publish_model_metrics,
)
from agent_py_agent.agent.conversation.store import ConversationStore


def _messages(count: int) -> list[dict]:
    return [{"role": "user" if idx % 2 == 0 else "assistant", "content": f"msg-{idx}"} for idx in range(count)]


def test_extra_checkpoint_positions_captures_exact_previous_boundary():
    rows = _messages(13)
    previous = request_surface({"model": "m", "messages": rows[:11]}, "endpoint")
    assert [row["index"] for row in previous["chain_checkpoints"]] == [11]

    default_current = request_surface({"model": "m", "messages": rows}, "endpoint")
    assert [row["index"] for row in default_current["chain_checkpoints"]] == [13]

    bounded_current = request_surface(
        {"model": "m", "messages": rows},
        "endpoint",
        extra_checkpoint_positions=(11, 11, 0, -2, True, 999),
    )
    assert [row["index"] for row in bounded_current["chain_checkpoints"]] == [11, 13]
    appended = compare_request_surfaces(previous, bounded_current)
    assert appended["changes"] == ["history_appended"]
    assert appended["comparable"] is True
    assert appended["shared_message_prefix"] == 11
    assert appended["changed_block_index"] is None

    mutated_rows = copy.deepcopy(rows)
    mutated_rows[9]["content"] = "edited-inside-boundary"
    mutated = request_surface(
        {"model": "m", "messages": mutated_rows},
        "endpoint",
        extra_checkpoint_positions=(11,),
    )
    prefix_changed = compare_request_surfaces(previous, mutated)
    assert prefix_changed["changes"] == ["history_prefix_changed"]
    assert prefix_changed["changed_block_index"] == 11
    assert prefix_changed["shared_message_prefix"] == 0


def test_extra_checkpoint_positions_respects_bounded_checkpoint_limit():
    rows = _messages(4096)
    previous = request_surface({"model": "m", "messages": rows[:4095]}, "endpoint")
    current = request_surface(
        {"model": "m", "messages": rows},
        "endpoint",
        extra_checkpoint_positions=(4095,),
    )
    indices = [row["index"] for row in current["chain_checkpoints"]]
    assert len(indices) == _CHAIN_CHECKPOINT_LIMIT_COUNT
    assert 4095 in indices and indices[-1] == 4096
    compared = compare_request_surfaces(previous, current)
    assert compared["changes"] == ["history_appended"]
    assert compared["comparable"] is True
    assert compared["shared_message_prefix"] == 4095


@pytest.mark.parametrize("mutation", [False, True])
def test_extra_checkpoint_separates_anthropic_cache_control_only_move(mutation: bool):
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": [{"type": "text", "text": f"turn-{i}"}]}
        for i in range(11)
    ]
    extended = copy.deepcopy(history) + [
        {"role": "assistant", "content": [{"type": "text", "text": "reply-11"}]},
        {"role": "user", "content": [{"type": "text", "text": "follow-12"}]},
    ]
    if mutation:
        extended[9]["content"][0]["text"] = "mutated-content"

    def _surface(rows: list[dict], *, extra: tuple[int, ...] = ()) -> dict[str, object]:
        msgs, _ = anthropic_messages_with_optional_cache(
            prompt="",
            messages=rows,
            tools=[],
            cache_enabled=True,
            stable_system_cache_active=True,
            structured_native_layout_active=True,
        )
        return request_surface(
            {"model": "claude-opus-5.5", "messages": msgs},
            "endpoint",
            extra_checkpoint_positions=extra,
        )

    previous = _surface(history)
    current = _surface(extended, extra=(11,))
    result = compare_request_surfaces(previous, current)
    assert result["changes"] == ["history_prefix_changed"]
    assert result["changed_block_index"] == 11
    assert result["content_prefix_comparable"] is True
    assert result["content_prefix_changed"] is mutation
    assert result["cache_control_only_prefix_changed"] is (not mutation)
    assert result["content_shared_message_prefix"] == (0 if mutation else 11)


def test_provider_attempt_observer_forwards_previous_message_count_http_and_ws(monkeypatch):
    payload = {"model": "m", "messages": _messages(13)}
    http_events: list[dict] = []

    class _FakeResp(BytesIO):
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    with patch(
        "agent_py_agent.agent.backends.gateway_helpers._gateway_urlopen",
        return_value=_FakeResp(b'{"ok": true}'),
    ):
        with provider_attempt_observer(
            http_events.append,
            cache_diagnostics=True,
            previous_message_count=11,
        ):
            assert current_observer_previous_message_count() == 11
            with provider_attempt_observer(lambda _evt: None, cache_diagnostics=False):
                assert current_observer_previous_message_count() is None
            assert current_observer_previous_message_count() == 11
            req = GatewayRequest(
                api_base="https://api.example.invalid",
                api_key="synthetic-key",
                path="/v1/messages",
                payload=payload,
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )
            gateway_helpers.post_json(req)

    assert current_observer_previous_message_count() is None
    http_started = next(evt for evt in http_events if evt.get("status") == "started")
    assert [row["index"] for row in http_started["request_surface"]["chain_checkpoints"]] == [11, 13]

    ws_events: list[dict] = []
    ws_req = GatewayRequest(
        api_base="https://api.example.invalid",
        api_key="synthetic-key",
        path="/v1/responses",
        payload={"model": "m", "stream": True, "input": _messages(13)},
        headers={"Content-Type": "application/json"},
        timeout=10.0,
    )
    with provider_attempt_observer(
        ws_events.append,
        cache_diagnostics=True,
        previous_message_count=11,
    ):
        ws_event = ws._attempt_event(ws_req, 1)
    assert [row["index"] for row in ws_event["request_surface"]["chain_checkpoints"]] == [11, 13]


def test_invoke_backend_generate_passes_previous_message_count_from_ledger():
    ledger = ModelCallLedger()
    ledger.started(
        ModelCallStartedParams(
            "call-t1-1",
            "fixture",
            "m",
            100,
            run_id="run-1",
            metadata={"thread_id": "thread-1"},
        )
    )
    ledger.provider_attempt(
        ModelCallProviderAttemptParams(
            "call-t1-1",
            "att-1",
            "started",
            request_surface=request_surface({"model": "m", "messages": _messages(11)}, "endpoint"),
        )
    )
    # 模拟同线程紧接着一次无 request_surface 的未发出调用，不应掩盖上一份有效 request_surface。
    ledger.started(
        ModelCallStartedParams(
            "call-t1-2",
            "fixture",
            "m",
            100,
            run_id="run-1",
            metadata={"thread_id": "thread-1"},
        )
    )
    ledger.started(
        ModelCallStartedParams(
            "call-t1-3",
            "fixture",
            "m",
            120,
            run_id="run-1",
            metadata={"thread_id": "thread-1"},
        )
    )

    seen_counts: list[int | None] = []

    class _FakeBackend:
        model_name = "m"

        def generate(self, prompt, **kwargs):
            seen_counts.append(current_observer_previous_message_count())
            return SimpleNamespace(text="ok", usage={})

    class _DummyLiveness:
        def run_if_current(self, operation):
            operation()
            return True

        def bind_display_waiter(self, waiter):
            return None

    def _state(call_id: str, thread_id: str, run_id: str) -> _ModelGenerationState:
        params = SimpleNamespace(
            run_id=run_id,
            task_id="",
            task_attributes={"conversation_thread_id": thread_id} if thread_id else {},
            live_archive_state={},
            effective_on_chunk=None,
        )
        return _ModelGenerationState(
            agent=SimpleNamespace(config=SimpleNamespace(my_agent_owner_id="owner-1"), backend=_FakeBackend()),
            params=params,
            chunk_filter=ToolBoundaryChunkFilter(lambda _c: None),
            ledger=ledger,
            call_id=call_id,
            tool_rounds=0,
            first_token_timeout_seconds=5.0,
            on_chunk=lambda _c: None,
            retry_sink=None,
            liveness=_DummyLiveness(),
        )

    _invoke_backend_generate(_FakeBackend(), "hi", _state("call-t1-3", "thread-1", "run-1"))
    assert seen_counts[-1] == 11

    # 不同线程不可借用 thread-1 的 message_count。
    ledger.started(
        ModelCallStartedParams(
            "call-t2-1",
            "fixture",
            "m",
            100,
            run_id="run-2",
            metadata={"thread_id": "thread-2"},
        )
    )
    _invoke_backend_generate(_FakeBackend(), "hi", _state("call-t2-1", "thread-2", "run-2"))
    assert seen_counts[-1] is None

    # thread_id 为空时按 run_id 匹配。
    ledger.started(ModelCallStartedParams("call-r3-1", "fixture", "m", 50, run_id="run-3", metadata={}))
    ledger.provider_attempt(
        ModelCallProviderAttemptParams(
            "call-r3-1",
            "att-r3",
            "started",
            request_surface=request_surface({"model": "m", "messages": _messages(7)}, "endpoint"),
        )
    )
    ledger.started(ModelCallStartedParams("call-r3-2", "fixture", "m", 60, run_id="run-3", metadata={}))
    _invoke_backend_generate(_FakeBackend(), "hi", _state("call-r3-2", "", "run-3"))
    assert seen_counts[-1] == 7


def test_model_metrics_suppresses_history_prefix_changed_for_cache_control_only(tmp_path):
    assert _add_cache_change_counts(
        None,
        {"changes": ["history_prefix_changed"], "cache_control_only_prefix_changed": True},
    ) == {}
    assert _add_cache_change_counts(
        None,
        {"changes": ["history_prefix_changed", "tools_changed"], "cache_control_only_prefix_changed": True},
    ) == {"tools_changed": 1}
    assert _add_cache_change_counts(
        None,
        {"changes": ["history_prefix_changed"], "cache_control_only_prefix_changed": False},
    ) == {"history_prefix_changed": 1}

    store = ConversationStore(tmp_path)
    thread = store.threads.get_or_create({"canonical_user_id": "test-user"})
    clock = SimpleNamespace(now=10.0)
    ledger = ModelCallLedger()
    params = SimpleNamespace(
        request_id="req-1",
        run_id="run-1",
        live_archive_state={},
        task_attributes={"conversation_thread_id": thread.thread_id},
        effective_on_chunk=None,
    )
    agent = SimpleNamespace(conversation_store=store, _model_call_ledger=ledger)

    def _run_call(call_id: str, rows: list[dict], *, extra: tuple[int, ...] = ()) -> dict:
        msgs, _ = anthropic_messages_with_optional_cache(
            prompt="",
            messages=rows,
            tools=[],
            cache_enabled=True,
            stable_system_cache_active=True,
            structured_native_layout_active=True,
        )
        surface = request_surface(
            {"model": "claude-opus-5.5", "messages": msgs},
            "endpoint",
            extra_checkpoint_positions=extra,
        )
        ledger.started(
            ModelCallStartedParams(
                call_id,
                "anthropic",
                "claude-opus-5.5",
                1000,
                request_id=params.request_id,
                run_id=params.run_id,
                metadata={"thread_id": thread.thread_id},
            )
        )
        clock.now += 1.0
        ledger.first_token(ModelCallFirstTokenParams(call_id))
        ledger.provider_attempt(
            ModelCallProviderAttemptParams(call_id, f"{call_id}-att", "started", request_surface=surface)
        )
        response = SimpleNamespace(
            text="done",
            usage={"input_tokens": 1000, "cache_read_input_tokens": 800, "output_tokens": 20},
        )
        from agent_py_agent.agent.agent_core.model.call_runtime import record_model_call_finished

        record_model_call_finished(ledger, call_id, response)
        return publish_model_metrics(agent, params, pending=False, response=response, call_id=call_id)

    base = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": [{"type": "text", "text": f"m-{i}"}]}
        for i in range(11)
    ]
    _run_call("c1", base)

    extended = copy.deepcopy(base) + [
        {"role": "assistant", "content": [{"type": "text", "text": "a-11"}]},
        {"role": "user", "content": [{"type": "text", "text": "u-12"}]},
    ]
    m2 = _run_call("c2", extended, extra=(11,))
    assert m2["cache_diagnostic"]["cache_control_only_prefix_changed"] is True
    assert "cache_change_counts" not in m2

    mutated = copy.deepcopy(extended) + [
        {"role": "assistant", "content": [{"type": "text", "text": "a-13"}]},
    ]
    mutated[4]["content"][0]["text"] = "real-rewrite"
    m3 = _run_call("c3", mutated, extra=(13,))
    assert m3["cache_diagnostic"]["cache_control_only_prefix_changed"] is False
    assert m3["cache_change_counts"] == {"history_prefix_changed": 1}
    reloaded = model_metrics_from_thread(ConversationStore(tmp_path), thread.thread_id)
    assert reloaded["cache_change_counts"] == {"history_prefix_changed": 1}
    assert set(reloaded["cache_change_counts"]) <= {
        "history_prefix_changed",
        "history_shortened",
        "history_appended",
        "reasoning_effort_changed",
        "thinking_changed",
        "tools_changed",
        "system_changed",
        "model_changed",
        "endpoint_changed",
        "options_changed",
    }


def test_request_surface_unwraps_responses_namespace_tools():
    inner_tools = [{"type": "function", "name": f"tool_{i}", "parameters": {"type": "object"}} for i in range(13)]
    payload = {
        "model": "gpt-6.1-sol",
        "input": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "namespace",
                "name": "my_agent",
                "description": "my-agent runtime tools",
                "tools": inner_tools,
            }
        ],
    }
    original = copy.deepcopy(payload)
    s1 = request_surface(payload, "https://provider.invalid/v1")
    assert payload == original
    assert s1["tool_count"] == 13
    assert "my_agent" not in json.dumps(s1)

    added = copy.deepcopy(payload)
    added["tools"][0]["tools"].append({"type": "function", "name": "tool_13"})
    s2 = request_surface(added, "https://provider.invalid/v1")
    assert s2["tool_count"] == 14
    assert s2["tool_names_digest"] != s1["tool_names_digest"]
    diff_add = compare_request_surfaces(s1, s2)
    assert {"tools_changed", "tools_names_changed"} <= set(diff_add["changes"])

    removed = copy.deepcopy(payload)
    removed["tools"][0]["tools"].pop()
    s3 = request_surface(removed, "https://provider.invalid/v1")
    assert s3["tool_count"] == 12
    assert s3["tool_names_digest"] != s1["tool_names_digest"]
    diff_rem = compare_request_surfaces(s1, s3)
    assert {"tools_changed", "tools_names_changed"} <= set(diff_rem["changes"])

    renamed = copy.deepcopy(payload)
    renamed["tools"][0]["tools"][3]["name"] = "tool_3_renamed"
    s4 = request_surface(renamed, "https://provider.invalid/v1")
    assert s4["tool_count"] == 13
    assert s4["tool_names_digest"] != s1["tool_names_digest"]

    mixed = {
        "tools": [
            {"type": "web_search_preview"},
            {"type": "namespace", "name": "my_agent", "tools": inner_tools},
        ]
    }
    assert request_surface(mixed, "endpoint")["tool_count"] == 14

    empty_ns = {"tools": [{"type": "namespace", "name": "my_agent", "tools": []}]}
    assert request_surface(empty_ns, "endpoint")["tool_count"] == 1

    flat = {"tools": [{"name": "a"}, {"name": "b"}]}
    assert request_surface(flat, "endpoint")["tool_count"] == 2
