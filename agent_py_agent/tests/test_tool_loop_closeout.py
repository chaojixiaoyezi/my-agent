"""收口请求归属、异常传播与延后裁决的因果验证。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as service
from agent_py_agent.agent.agent_core.tool_loop.closeout import (
    generate_tool_loop_closeout,
    unknown_outcome_runtime_reason,
)
from agent_py_agent.agent.backends import ModelResponse


def test_closeout_preserves_request_response_pair_and_reads_reason_after_strip():
    events = []
    state = {"reason": "before_request"}
    sampled = ModelResponse("原文", "fake", usage={"output_tokens": 7})
    stripped = replace(sampled, text="剥离后的真实回复")

    def build():
        events.append("build")
        return "本次出站请求"

    def generate(prompt):
        assert prompt == "本次出站请求"
        events.append("generate")
        state["reason"] = "before_strip"
        return sampled

    def strip(response):
        assert response is sampled
        events.append("strip")
        state["reason"] = "after_strip"
        return stripped

    def reason():
        events.append("reason")
        return state["reason"]

    prompt, result = generate_tool_loop_closeout(
        build_prompt=build, generate_response=generate,
        prepare_handoff_response=strip, read_runtime_reason=reason,
    )
    assert events == ["build", "generate", "strip", "reason"]
    assert prompt == "本次出站请求"
    assert result.text == stripped.text and result.usage == sampled.usage
    assert result.runtime_status == "unfinished"
    assert result.runtime_reason == "after_strip"
    assert result.runtime_source == "tool_loop"
    assert sampled.text == "原文" and sampled.runtime_reason == ""
    assert stripped.runtime_reason == ""


@pytest.mark.parametrize("failed_at", ["build", "generate", "strip", "reason"])
@pytest.mark.parametrize("error_type", [RuntimeError, InterruptedError])
def test_closeout_failure_propagates_without_retry_or_later_side_effect(failed_at, error_type):
    events = []
    failure = error_type("本次收口终止")

    def stage(name, value):
        events.append(name)
        if name == failed_at:
            raise failure
        return value

    with pytest.raises(error_type) as caught:
        generate_tool_loop_closeout(
            build_prompt=lambda: stage("build", "请求"),
            generate_response=lambda prompt: stage("generate", ModelResponse(prompt, "fake")),
            prepare_handoff_response=lambda response: stage("strip", response),
            read_runtime_reason=lambda: stage("reason", "TOOL_ACTION_NOT_REQUIRED"),
        )
    assert caught.value is failure
    order = ["build", "generate", "strip", "reason"]
    assert events == order[:order.index(failed_at) + 1]


@pytest.mark.parametrize(
    ("strip_reason", "runtime_reason"),
    [("", "TOOL_ROUND_LIMIT_REACHED"),
     ("repeated_failure_exhausted", "REPEATED_TOOL_FAILURE_EXHAUSTED"),
     ("unknown_outcome", "TOOL_OPERATION_OUTCOME_UNKNOWN"),
     ("no_action_gate", "TOOL_ACTION_NOT_REQUIRED")],
)
def test_host_binding_keeps_identity_and_reads_live_halt_after_generation(
    monkeypatch, strip_reason, runtime_reason,
):
    agent = object()
    params = SimpleNamespace(
        unknown_outcome_halt=("write", "COMMAND_FAILED", "not_started", False),
    )
    sampled = ModelResponse("交接", "fake", usage={"input_tokens": 19})
    events = []

    def build(actual_agent, actual_params):
        assert actual_agent is agent and actual_params is params
        events.append("build")
        return "当前请求"

    def generate(request):
        assert request.agent is agent and request.params is params
        assert request.prompt == "当前请求" and request.tool_rounds == 3
        events.append("generate")
        return sampled

    def strip(actual_params, response, reason):
        assert actual_params is params and response is sampled
        assert reason == strip_reason
        params.unknown_outcome_halt = ("write", "COMMAND_FAILED", "unknown", True)
        events.append("strip")
        return response

    monkeypatch.setattr(service, "build_tool_loop_prompt", build)
    monkeypatch.setattr(service, "generate_model_response", generate)
    monkeypatch.setattr(service, "without_tool_call_after_limit", strip)
    prompt, result = service._final_response_after_halt(
        agent, params, 3, reason=strip_reason, runtime_reason=runtime_reason,
    )
    assert prompt == "当前请求" and events == ["build", "generate", "strip"]
    assert result.runtime_reason == runtime_reason
    assert result.runtime_status == "unfinished" and result.usage == {"input_tokens": 19}


def test_unknown_effect_cannot_become_retryable_when_contract_lookup_fails(monkeypatch):
    from agent_py_agent.agent.contracts import error_taxonomy

    def unavailable(_code):
        raise OSError("合同暂不可读")

    monkeypatch.setattr(error_taxonomy, "error_contract", unavailable)
    assert unknown_outcome_runtime_reason(
        ("write", "COMMAND_FAILED", "not_started", False),
    ) == "TOOL_OPERATION_OUTCOME_UNKNOWN"
