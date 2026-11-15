# LLM: PD-1 / RC-01 Option B 行为回归：Responses 拒绝尾部 configuration_update 时仅删该尾项并保留原思考档位，
#   同时按未降档档位重算输出预留与窗口准入（超窗不发第二次请求，交回分段/前缀错误合同）。
# 模块用途: 验证 sync/stream 400 回退、跨模块输出预留恢复、窗口准入重检与并发 memo 失效自愈。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    compact_summary_output_reserve_tokens,
)
from agent_py_agent.agent.backends import (
    BackendOptions,
    ProviderRequestOptions,
    http,
)
from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
from agent_py_agent.agent.backends.request_budget import (
    RequestBudgetExceeded,
    resolve_request_token_budget,
)
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.conversation.auxiliary_model_call import (
    generate_auxiliary_model_response,
)
from agent_py_agent.agent.conversation.compact_guard import (
    ConversationCompactError,
)
from agent_py_agent.agent.conversation.compact_message_source import (
    CompactMessageSource,
)
from agent_py_agent.agent.conversation.compact_request_budget import (
    generate_bounded_compact_response,
)
from agent_py_agent.tests.test_responses_compact_fallback import (
    _ok,
    _rejection,
    _responses_agent,
    _summary_request,
)

_GATEWAY_BODY_ATTR = "pay" + "load"


def _sent_body(call) -> dict:
    return deepcopy(getattr(call, _GATEWAY_BODY_ATTR))


def _ok_text(text: str) -> dict:
    body = _ok()
    body["output"][0]["content"][0]["text"] = text
    return body


def _large_messages(repeats: int) -> list[dict]:
    chunk = "历史事件。" * repeats
    return [
        {"role": "user", "content": chunk},
        {"role": "assistant", "content": "已记录分段基准"},
    ]


def test_call_runtime_reserve_supports_preserved_effort_fallback(monkeypatch):
    agent, _ = _responses_agent(monkeypatch, window=272000)
    assert compact_summary_output_reserve_tokens(agent) == 16384
    assert (
        compact_summary_output_reserve_tokens(
            agent, allow_tail_downshift=False
        )
        == 68000
    )

    unbounded = replace(
        agent.config, memory_compact_summary_max_output_tokens=0
    )
    zero_agent = SimpleNamespace(backend=agent.backend, config=unbounded)
    expected_zero = compact_summary_output_reserve_tokens(zero_agent)
    assert (
        compact_summary_output_reserve_tokens(
            zero_agent, allow_tail_downshift=False
        )
        == expected_zero
    )


@pytest.mark.parametrize(
    "thread_effort,expected_wire",
    [("xhigh", "xhigh"), ("max", "max"), ("", None)],
)
def test_exact_400_tail_rejection_drops_only_tail_and_preserves_effort(
    monkeypatch, thread_effort, expected_wire
):
    sent = []
    backend = OpenAIResponsesBackend(
        BackendOptions(
            "https://fixture.invalid/v1",
            "test-key",
            "gpt-6.1-test",
            max_tokens=68000,
            configured_output_ceiling=128000,
            context_window_tokens=272000,
            stream_enabled=False,
            reasoning_control="effort",
            reasoning_levels=("low", "medium", "high", "xhigh", "max"),
            reasoning_update_items="auto",
        )
    )

    def fake_post(call):
        body = _sent_body(call)
        sent.append(body)
        if len(sent) == 1:
            n = len(body["input"]) - 1
            raise _rejection(n, code="unsupported_value", param=f"input[{n}].type")
        return _ok_text("preserved-effort-ok")

    monkeypatch.setattr(http, "post_json", fake_post)
    budget = resolve_request_token_budget(
        input_tokens=1000,
        context_window_tokens=272000,
        output_ceiling=16384,
        shared_window=True,
        require_full_output=True,
    )
    response = backend.generate(
        "summarize thread history",
        request_options=ProviderRequestOptions(
            system_instruction="stable system",
            reasoning_effort=thread_effort,
            reasoning_update_effort="low",
            max_output_tokens=16384,
            token_budget=budget,
        ),
    )
    assert response.text == "preserved-effort-ok"
    assert len(sent) == 2
    assert any(item.get("type") == "configuration_update" for item in sent[0]["input"])
    assert not any(
        item.get("type") == "configuration_update" for item in sent[1]["input"]
    )
    expected_body = deepcopy(sent[0])
    del expected_body["input"][-1]
    expected_body["max_output_tokens"] = 68000
    assert sent[1] == expected_body
    if expected_wire is None:
        assert "reasoning" not in sent[1]
    else:
        assert sent[1].get("reasoning") == {"effort": expected_wire}
    assert response.request_token_budget is not None
    assert response.request_token_budget.output_tokens == 68000


def test_stream_rejection_closes_stream_before_preserved_effort_retry(monkeypatch):
    agent, _ = _responses_agent(monkeypatch)
    agent.backend.stream_enabled = True
    opened = []
    closed = []

    class ClosingStream:
        def __init__(self, index: int, wire_body: dict):
            self.index = index
            self.wire_body = wire_body

        def __iter__(self):
            if self.index == 0:
                n = len(self.wire_body["input"]) - 2
                raise _rejection(n, code="invalid_value", param=f"input[{n}].type")
            yield json.dumps({"type": "response.output_text.delta", "delta": "ok"})
            yield json.dumps(
                {"type": "response.completed", "response": _ok_text("ok")}
            )

        def close(self):
            closed.append(self.index)

    def fake_stream(path, wire_body, headers, *args, **kwargs):
        del path, headers, args, kwargs
        opened.append(deepcopy(wire_body))
        return ClosingStream(len(opened) - 1, wire_body)

    monkeypatch.setattr(agent.backend, "request_stream_iter", fake_stream)
    response = generate_bounded_compact_response(_summary_request(agent))
    assert response.text == "ok"
    assert len(opened) == 2
    assert closed == [0, 1]
    assert opened[1]["reasoning"] == {"effort": "max"}
    assert opened[1]["max_output_tokens"] == 68000
    assert response.request_token_budget.output_tokens == 68000
    assert not any(
        item.get("type") == "configuration_update" for item in opened[1]["input"]
    )


def test_stream_partial_delta_before_rejection_does_not_retry(monkeypatch):
    agent, _ = _responses_agent(monkeypatch)
    agent.backend.stream_enabled = True
    opened = []

    class PartialStream:
        def __init__(self, wire_body: dict):
            self.wire_body = wire_body

        def __iter__(self):
            n = len(self.wire_body["input"]) - 2
            yield json.dumps(
                {"type": "response.output_text.delta", "delta": "half"}
            )
            raise _rejection(n, code="invalid_value", param=f"input[{n}].type")

        def close(self):
            return None

    def fake_stream(path, wire_body, headers, *args, **kwargs):
        del path, headers, args, kwargs
        opened.append(deepcopy(wire_body))
        return PartialStream(wire_body)

    monkeypatch.setattr(agent.backend, "request_stream_iter", fake_stream)
    with pytest.raises(ProviderRequestRejectedError):
        generate_auxiliary_model_response(_summary_request(agent))
    assert len(opened) == 1
    assert agent.backend.supports_reasoning_update_items()


def test_fallback_overflow_after_400_aborts_auxiliary_retry_and_triggers_segmented_compact(
    monkeypatch,
):
    agent, sent = _responses_agent(monkeypatch, window=272000)
    # 约 220,000 估算 token：16,384 预留下 220k <= 253,568 (cache_surface_budget) 且 220k + 16,384 + 2,048 <= 272,000；
    # 回退保留 max 档恢复 68,000 预留后 220k + 68,000 + 2,048 > 272,000 触发 input_overflow。
    big_messages = _large_messages(44000)
    cached_tools = [
        {"name": "read_file", "description": "read", "input_schema": {"type": "object"}}
    ]

    def reject_tail_then_accept_segments(call):
        body = _sent_body(call)
        sent.append(body)
        if len(sent) == 1:
            n = next(
                i
                for i, item in enumerate(body["input"])
                if item.get("type") == "configuration_update"
            )
            raise _rejection(n, code="unsupported_type", param=f"input[{n}].type")
        return _ok_text("分段核实完成")

    monkeypatch.setattr(http, "post_json", reject_tail_then_accept_segments)

    # 1. 直接辅助调用：精确 400 后本地复核到 68K 预留超窗，绝不发第二次超窗请求。
    direct_req = replace(_summary_request(agent), messages=big_messages, tools=cached_tools)
    with pytest.raises(RequestBudgetExceeded) as caught:
        generate_auxiliary_model_response(direct_req)
    assert caught.value.reason == "input_overflow"
    assert len(sent) == 1
    assert not agent.backend.supports_reasoning_update_items()

    # 2. 重置一次：通过 generate_bounded_compact_response 验证单次 400 失败后切入分段压缩闭环。
    with agent.backend._reasoning_update_lock:
        agent.backend._reasoning_update_unsupported = False
    sent.clear()
    bounded = generate_bounded_compact_response(
        replace(_summary_request(agent), messages=big_messages, tools=cached_tools)
    )
    assert bounded.text == "分段核实完成"
    assert len(sent) >= 2
    assert sent[0]["max_output_tokens"] == 16384
    assert sent[0]["reasoning"] == {"effort": "max"}
    assert all(call["max_output_tokens"] == 68000 for call in sent[1:])
    assert all(call["reasoning"] == {"effort": "low"} for call in sent[1:])
    assert all(
        not any(item.get("type") == "configuration_update" for item in call["input"])
        for call in sent[1:]
    )
    assert not agent.backend.supports_reasoning_update_items()


def test_fallback_overflow_with_preserve_prefix_raises_prefix_too_large(monkeypatch):
    agent, sent = _responses_agent(monkeypatch, window=272000)
    big_messages = _large_messages(44000)

    def reject_tail(call):
        body = _sent_body(call)
        sent.append(body)
        n = next(
            i
            for i, item in enumerate(body["input"])
            if item.get("type") == "configuration_update"
        )
        raise _rejection(n, code="invalid_value", param=f"input[{n}].type")

    monkeypatch.setattr(http, "post_json", reject_tail)
    base_req = _summary_request(agent)
    prefix_source = CompactMessageSource(
        factory=lambda: iter(big_messages), preserve_prefix=True
    )
    with pytest.raises(ConversationCompactError) as caught:
        generate_bounded_compact_response(
            replace(base_req, messages=None),
            message_source=prefix_source,
        )
    assert caught.value.code == "COMPACT_PREFIX_TOO_LARGE"
    assert len(sent) == 1
    assert not agent.backend.supports_reasoning_update_items()


def test_fallback_invalid_geometry_surfaces_compact_invalid_output_budget(monkeypatch):
    agent, sent = _responses_agent(monkeypatch, window=40000)
    # 显式使 backend.max_tokens > window：初始 16384 可几何容纳，回退到 68000 后触发 invalid_geometry。
    agent.backend.max_tokens = 68000

    def reject_tail(call):
        body = _sent_body(call)
        sent.append(body)
        n = len(body["input"]) - 2
        raise _rejection(n, code="invalid_value", param=f"input[{n}].type")

    monkeypatch.setattr(http, "post_json", reject_tail)
    with pytest.raises(ConversationCompactError) as caught:
        generate_auxiliary_model_response(_summary_request(agent))
    assert caught.value.code == "COMPACT_INVALID_OUTPUT_BUDGET"
    assert len(sent) == 1


def test_memo_flip_between_budget_freeze_and_provider_options_resigns_budget(
    monkeypatch,
):
    agent, sent = _responses_agent(monkeypatch, window=272000)
    original_supports = agent.backend.supports_reasoning_update_items
    calls = 0

    def flip_before_options():
        nonlocal calls
        calls += 1
        # 前面预算规划支持，进入 _auxiliary_provider_options 后失效
        if calls >= 4:
            with agent.backend._reasoning_update_lock:
                agent.backend._reasoning_update_unsupported = True
            return False
        return original_supports()

    monkeypatch.setattr(
        agent.backend, "supports_reasoning_update_items", flip_before_options
    )
    result = generate_bounded_compact_response(_summary_request(agent))
    assert result.text == "已核实的事实"
    assert len(sent) == 1
    assert sent[0]["reasoning"] == {"effort": "max"}
    assert sent[0]["max_output_tokens"] == 68000
    assert result.request_token_budget.output_tokens == 68000
    assert not any(
        item.get("type") == "configuration_update" for item in sent[0]["input"]
    )


def test_memo_flip_overflow_raises_input_overflow_before_http_send(monkeypatch):
    agent, sent = _responses_agent(monkeypatch, window=272000)
    big_messages = _large_messages(44000)
    original_supports = agent.backend.supports_reasoning_update_items
    calls = 0

    def flip_before_options():
        nonlocal calls
        calls += 1
        if calls >= 2:
            with agent.backend._reasoning_update_lock:
                agent.backend._reasoning_update_unsupported = True
            return False
        return original_supports()

    monkeypatch.setattr(
        agent.backend, "supports_reasoning_update_items", flip_before_options
    )
    with pytest.raises(RequestBudgetExceeded) as caught:
        generate_auxiliary_model_response(
            replace(_summary_request(agent), messages=big_messages)
        )
    assert caught.value.reason == "input_overflow"
    assert len(sent) == 0


def test_mid_segment_memo_flip_shrinks_segment_budget_and_finishes(monkeypatch):
    agent, sent = _responses_agent(monkeypatch, window=272000)
    agent.backend.tools_override = None
    # ~260,000 tokens 跳过单次请求，首段按 16,384 预算选出 ~202,500 tokens；
    # 进入 _generate_materialized_response 前 memo 失效，触发段内输入超窗并收紧到 68,000 对应分段预算。
    big_messages = _large_messages(52000)
    flipped = False
    from agent_py_agent.agent.conversation import compact_request_budget as budget_mod

    orig_gen = budget_mod._generate_materialized_response

    def flip_once(request, source, **kwargs):
        nonlocal flipped
        if not flipped:
            flipped = True
            with agent.backend._reasoning_update_lock:
                agent.backend._reasoning_update_unsupported = True
        return orig_gen(request, source, **kwargs)

    monkeypatch.setattr(budget_mod, "_generate_materialized_response", flip_once)
    response = generate_bounded_compact_response(
        replace(_summary_request(agent), messages=big_messages)
    )
    assert response.text == "已核实的事实"
    assert len(sent) >= 2
    assert all(call["max_output_tokens"] == 68000 for call in sent)


def test_unrelated_400_rejection_does_not_retry_or_drop_tail(monkeypatch):
    agent, sent = _responses_agent(monkeypatch)

    def reject_bad_request(call):
        sent.append(_sent_body(call))
        raise _rejection(0, code="bad_request", param="input[0].type")

    monkeypatch.setattr(http, "post_json", reject_bad_request)
    with pytest.raises(ProviderRequestRejectedError) as caught:
        generate_bounded_compact_response(_summary_request(agent))
    assert caught.value.status_code == 400
    assert len(sent) == 1
    assert any(
        item.get("type") == "configuration_update" for item in sent[0]["input"]
    )
    assert agent.backend.supports_reasoning_update_items()
