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


@pytest.mark.parametrize(
    ("final", "expected"),
    [
        (ModelResponse("重试成功", "fake"), ["account", "ack"]),
        (ModelResponse("", "fake", runtime_status="context_overflow", runtime_source="provider_error"),
         ["account", "restore"]),
    ],
)
def test_transient_retry_reselects_and_accepts_only_final_attempt_params(monkeypatch, final, expected):
    """瞬断后第二次尝试重新选模；计量、恢复、确认只收到最终成功那次尝试的参数。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.model_request_selection import model_request_selection_scope

    agent = SimpleNamespace(runtime_guard_policy=None)
    loop_params = SimpleNamespace(effective_on_chunk=None, live_archive_state=None)
    selected, events = [], []
    failure = ProviderTransientError("首次尝试瞬断")

    # 每次尝试都产出新的参数对象，模拟宿主在第二次尝试重新选模。
    class _ReselectingHost:
        def select(self, _agent, params, prompt):
            assert params is loop_params
            choice = SimpleNamespace(label=f"attempt-{len(selected) + 1}")
            selected.append(choice)
            return choice, f"{prompt}@{choice.label}"

    def request(_agent, params, _rounds, *, first_prompt=None, consumes_task_tool_surface=True):
        assert params is selected[-1] and first_prompt == f"原请求@{params.label}"
        if len(selected) == 1:
            raise failure
        return first_prompt, final

    monkeypatch.setattr(retry, "provider_transient_retry_delays", lambda _policy: (1.0,))
    monkeypatch.setattr(retry, "wait_interruptibly", lambda _delay: None)
    monkeypatch.setattr(service, "stale_subagent_attempt_message", lambda _agent: None)
    monkeypatch.setattr(service, "begin_goal_model_turn", lambda _agent, _params: None)
    monkeypatch.setattr(service, "_discard_stale_natural_reply_for_pending_turn_input",
                        lambda _agent, _params: False)
    monkeypatch.setattr(service, "natural_user_reply_model_params", lambda params: params)
    monkeypatch.setattr(service, "build_tool_loop_prompt", lambda _agent, _params: "原请求")
    monkeypatch.setattr(model_selection, "select_first_request_model",
                        lambda _agent, params, prompt: (params, prompt))
    monkeypatch.setattr(service, "_request_tool_loop_model_response", request)
    monkeypatch.setattr(service, "account_goal_model_response",
                        lambda _agent, params, response: events.append(("account", params, response)))
    monkeypatch.setattr(service, "restore_injected_turn_input_for_provider_retry",
                        lambda _agent, params: events.append(("restore", params)))
    monkeypatch.setattr(service, "acknowledge_injected_turn_input",
                        lambda _agent, params: events.append(("ack", params)))

    with model_request_selection_scope(_ReselectingHost()):
        outcome = service._model_turn_or_retry(agent, loop_params, 0, 0)

    first, second = selected
    assert outcome.params is second and outcome.response is final
    assert outcome.prompt == "原请求@attempt-2"
    assert [event[0] for event in events] == expected
    assert all(event[1] is second for event in events)
    assert not any(event[1] is first for event in events)
    assert events[0][2] is final
