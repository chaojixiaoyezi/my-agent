"""模型采纳顺序及瞬断后输入不可重复发送的合同验证。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry
from agent_py_agent.agent.agent_core.runtime.guidance import (
    active_turn_input_has_unconfirmed_delivery,
)
from agent_py_agent.agent.agent_core.tool_loop.model_turn import sample_and_accept_model_response
from agent_py_agent.agent.backends import ModelResponse, ProviderTransientError


@pytest.mark.parametrize(
    ("status", "source", "expected"),
    [
        ("ok", "", ["account", "ack"]),
        ("interrupted", "", ["account", "ack"]),
        ("context_overflow", "provider_error", ["account", "restore"]),
        ("context_overflow", "preflight", ["account"]),
        ("context_overflow", "", ["account"]),
    ],
)
def test_acceptance_preserves_response_identity_and_input_order(status, source, expected):
    events = []
    response = ModelResponse("", "fake", runtime_status=status, runtime_source=source)
    response.usage = {"input_tokens": 13, "output_tokens": 7}

    def account(actual):
        assert actual is response
        assert actual.usage == {"input_tokens": 13, "output_tokens": 7}
        events.append("account")

    prompt, actual = sample_and_accept_model_response(
        lambda: ("原请求", response),
        retry_allowed=lambda: True,
        account_response=account,
        restore_rejected_input=lambda: events.append("restore"),
        acknowledge_input=lambda: events.append("ack"),
    )
    assert prompt == "原请求"
    assert actual is response
    assert events == expected


@pytest.mark.parametrize("failure_at", ["request", "account"])
def test_failed_request_or_accounting_never_consumes_input(failure_at):
    events = []
    error = ValueError("停止采纳")

    def request():
        if failure_at == "request":
            raise error
        return "请求", ModelResponse("结果", "fake")

    def account(_response):
        events.append("account")
        raise error

    with pytest.raises(ValueError) as caught:
        sample_and_accept_model_response(
            request,
            retry_allowed=lambda: True,
            account_response=account,
            restore_rejected_input=lambda: events.append("restore"),
            acknowledge_input=lambda: events.append("ack"),
        )
    assert caught.value is error
    assert events == ([] if failure_at == "request" else ["account"])


@pytest.mark.parametrize("submitted", ["none", "submission", "pending"])
def test_transient_retry_reads_current_delivery_state_before_replay(monkeypatch, submitted):
    state = {}
    events = []
    response = ModelResponse("重试成功", "fake", usage={"output_tokens": 7})
    failure = ProviderTransientError("临时服务错误")
    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _policy: (1.0,))
    monkeypatch.setattr(retry, "wait_interruptibly", lambda _delay: events.append("wait"))

    def request():
        events.append("request")
        if events.count("request") == 1:
            if submitted == "submission":
                state["_guidance_submission_id"] = "submission-1"
            elif submitted == "pending":
                state["_guidance_ack_ids"] = {"message-1"}
            raise failure
        return "重试后的原请求", response

    def sample():
        return sample_and_accept_model_response(
            request,
            retry_allowed=lambda: not active_turn_input_has_unconfirmed_delivery(state),
            account_response=lambda actual: events.append(("account", actual)),
            restore_rejected_input=lambda: events.append("restore"),
            acknowledge_input=lambda: events.append("ack"),
        )

    if submitted == "none":
        prompt, actual = sample()
        assert prompt == "重试后的原请求"
        assert actual is response
        assert events == ["request", "wait", "request", ("account", response), "ack"]
    else:
        with pytest.raises(ProviderTransientError) as caught:
            sample()
        assert caught.value is failure
        assert events == ["request"]
        assert active_turn_input_has_unconfirmed_delivery(state)
