"""模型采纳顺序及瞬断后输入不可重复发送的合同验证。"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry
from agent_py_agent.agent.agent_core.runtime.guidance import (
    active_turn_input_has_unconfirmed_delivery,
)
from agent_py_agent.agent.agent_core.tool_loop.model_turn import (
    ModelTurnRequest,
    sample_and_accept_model_response,
)
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

    params = object()

    def account(actual_params, actual):
        assert actual_params is params
        assert actual is response
        assert actual.usage == {"input_tokens": 13, "output_tokens": 7}
        events.append("account")

    def restore(actual_params):
        assert actual_params is params
        events.append("restore")

    def ack(actual_params):
        assert actual_params is params
        events.append("ack")

    turn = sample_and_accept_model_response(
        lambda: ModelTurnRequest("原请求", response, params),
        retry_allowed=lambda: True,
        account_response=account,
        restore_rejected_input=restore,
        acknowledge_input=ack,
    )
    assert turn.prompt == "原请求"
    assert turn.response is response
    assert turn.params is params
    assert events == expected


@pytest.mark.parametrize("failure_at", ["request", "account"])
def test_failed_request_or_accounting_never_consumes_input(failure_at):
    events = []
    error = ValueError("停止采纳")

    def request():
        if failure_at == "request":
            raise error
        return ModelTurnRequest("请求", ModelResponse("结果", "fake"), object())

    def account(_params, _response):
        events.append("account")
        raise error

    with pytest.raises(ValueError) as caught:
        sample_and_accept_model_response(
            request,
            retry_allowed=lambda: True,
            account_response=account,
            restore_rejected_input=lambda _params: events.append("restore"),
            acknowledge_input=lambda _params: events.append("ack"),
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
        return ModelTurnRequest("重试后的原请求", response, state)

    def sample():
        return sample_and_accept_model_response(
            request,
            retry_allowed=lambda: not active_turn_input_has_unconfirmed_delivery(state),
            account_response=lambda _params, actual: events.append(("account", actual)),
            restore_rejected_input=lambda _params: events.append("restore"),
            acknowledge_input=lambda _params: events.append("ack"),
        )

    if submitted == "none":
        turn = sample()
        assert turn.prompt == "重试后的原请求"
        assert turn.response is response
        assert events == ["request", "wait", "request", ("account", response), "ack"]
    else:
        with pytest.raises(ProviderTransientError) as caught:
            sample()
        assert caught.value is failure
        assert events == ["request"]
        assert active_turn_input_has_unconfirmed_delivery(state)


@pytest.mark.parametrize(
    ("source", "retry_limit", "reclaim", "expected_calls", "expected_restores"),
    [("provider_error", 2, True, 3, 2), ("provider_error", 0, True, 1, 0),
     ("provider_error", 2, False, 1, 1), ("preflight", 2, True, 1, 0)],
)
def test_request_recovery_order_and_final_prompt_pair(
    source, retry_limit, reclaim, expected_calls, expected_restores
):
    from agent_py_agent.agent.agent_core.tool_loop.model_turn import request_model_response

    events = []
    prompts = []
    responses = []
    loaded = {"read_file"}

    def build():
        prompt = f"请求{len(prompts) + 1}"
        prompts.append(prompt)
        events.append(("build", prompt))
        return prompt

    def generate(prompt):
        assert prompt is prompts[-1]
        events.append(("generate", prompt))
        response = ModelResponse("", "fake", runtime_status="context_overflow", runtime_source=source)
        if len(responses) == 2:
            response = ModelResponse("恢复成功", "fake")
        responses.append(response)
        return response

    def limit():
        assert len(responses) == 1
        events.append("limit")
        return retry_limit

    def recover(prompt):
        assert events[-1] == "restore"
        assert prompt is prompts[-1]
        events.append(("recover", prompt))
        return reclaim

    prompt, response = request_model_response(
        build_prompt=build,
        generate_response=generate,
        restore_rejected_input=lambda: events.append("restore"),
        recover_context=recover,
        read_overflow_retry_limit=limit,
        visible_loaded_tools=loaded,
    )
    assert len(responses) == expected_calls
    assert prompt is prompts[-1] and response is responses[-1]
    assert events[:3] == [("build", "请求1"), ("generate", "请求1"), "limit"]
    assert events.count("limit") == 1
    assert events.count("restore") == expected_restores
    assert loaded == (set() if expected_calls == 3 else {"read_file"})


def test_request_failure_does_not_consume_visible_tools():
    from agent_py_agent.agent.agent_core.tool_loop.model_turn import request_model_response

    loaded = {"read_file"}
    failure = ProviderTransientError("暂未响应")

    def generate(_prompt):
        raise failure

    with pytest.raises(ProviderTransientError) as caught:
        request_model_response(
            build_prompt=lambda: "请求",
            generate_response=generate,
            restore_rejected_input=lambda: pytest.fail("不能恢复未知请求"),
            recover_context=lambda _prompt: pytest.fail("不能压缩未知请求"),
            read_overflow_retry_limit=lambda: pytest.fail("响应返回前不能读取上限"),
            visible_loaded_tools=loaded,
        )
    assert caught.value is failure
    assert loaded == {"read_file"}
