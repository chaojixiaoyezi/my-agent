"""只按真实结构化 HTTP 拒绝回退，不按异常文案猜能力。"""
import io
import json
from copy import deepcopy
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.errors import ProviderRequestRejectedError
from agent_py_agent.agent.backends.gateway_helpers import _runtime_http_error
from agent_py_agent.agent.conversation.compact_request_budget import (
    generate_bounded_compact_response,
)
from agent_py_agent.tests.test_compact_output_policy import _responses_agent, _summary_request


def _rejection(index, *, code="invalid_value", status=400, param=None):
    body = json.dumps({"error": {"message": "Unsupported configuration_update type",
        "code": code, "param": param if param is not None else f"input[{index}].type"}}).encode()
    return _runtime_http_error(HTTPError("https://fixture.invalid/responses", status,
        "provider rejection", {}, io.BytesIO(body)))


def _ok():
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": "已核实的事实"}]}]}


def _with_historical_wire_update(monkeypatch, backend):
    # canonical IR不持久化局部控制项；在最终发送边界提供真实旧wire项，不能用会被native投影忽略的字典冒充覆盖。
    original = backend._send_with_reasoning_update_fallback
    update = {"type": "configuration_update", "reasoning": {"effort": "high"}}

    def send(payload, headers, request, *, inserted_index):
        payload = {**payload, "input": [deepcopy(update), *payload["input"]]}
        return original(payload, headers, request, inserted_index=inserted_index + 1)

    monkeypatch.setattr(backend, "_send_with_reasoning_update_fallback", send)
    return update


def _reject_first(monkeypatch, *, mode="auto", fail_second=False):
    agent, _ = _responses_agent(monkeypatch, mode=mode)
    sent, errors = [], []

    def send(request):
        sent.append(deepcopy(request.payload))
        if len(sent) == 1:
            index = next(i for i, item in enumerate(request.payload["input"])
                         if item.get("type") == "configuration_update")
            error = _rejection(index)
            errors.append(error)
            raise error
        if fail_second:
            raise ProviderRequestRejectedError("second error", status_code=403)
        return _ok()

    monkeypatch.setattr(http, "post_json", send)
    return agent, sent, errors


def test_structured_rejection_retries_once_and_memoizes(monkeypatch):
    agent, sent, errors = _reject_first(monkeypatch)
    try:
        response = generate_bounded_compact_response(_summary_request(agent))
    except ProviderRequestRejectedError:
        response = None
    assert response is not None, "精确的新尾项拒绝必须有且只有一次兼容回退"
    assert response.text == "已核实的事实"
    assert len(sent) == 2
    assert sent[0]["reasoning"] == {"effort": "max"}
    assert sent[1]["reasoning"] == {"effort": "low"}
    assert sent[0]["max_output_tokens"] == sent[1]["max_output_tokens"] == 16384
    assert errors[0].details["provider_error"]["error"]["code"] == "invalid_value"
    assert not agent.backend.supports_reasoning_update_items()
    generate_bounded_compact_response(_summary_request(agent))
    assert len(sent) == 3
    # 已失败调用的兼容重放保留low意图；后续新调用按r4恢复线程分区和旧输出预留。
    assert sent[-1]["reasoning"] == {"effort": "max"}
    assert sent[-1]["max_output_tokens"] == agent.backend.max_tokens == 68000
    assert not any(item.get("type") == "configuration_update" for item in sent[-1]["input"])


def test_on_is_strict_and_does_not_memoize_rejection(monkeypatch):
    agent, sent, errors = _reject_first(monkeypatch, mode="on")
    with pytest.raises(ProviderRequestRejectedError) as caught:
        generate_bounded_compact_response(_summary_request(agent))
    assert caught.value is errors[0]
    assert len(sent) == 1
    assert agent.backend.supports_reasoning_update_items()


def test_off_preserves_thread_effort_without_tail_or_probe(monkeypatch):
    agent, sent = _responses_agent(monkeypatch, mode="off")
    response = generate_bounded_compact_response(_summary_request(agent))
    assert response.text and len(sent) == 1
    assert sent[0]["reasoning"] == {"effort": "max"}
    assert sent[0]["max_output_tokens"] == agent.backend.max_tokens == 68000
    assert not any(item.get("type") == "configuration_update" for item in sent[0]["input"])


@pytest.mark.parametrize("code,status,param", [
    ("bad_request", 400, "input[{n}].type"), ("invalid_value", 401, "input[{n}].type"),
    ("invalid_value", 403, "input[{n}].type"), ("invalid_value", 429, "input[{n}].type"),
    ("invalid_value", 500, "input[{n}].type"), ("invalid_value", 400, "input[0].type"),
    ("invalid_value", 400, "input[{n}].type.extra"), ("invalid_value", 400, "prefix.input[{n}].type"),
    ("invalid_value", 400, "input[-1].type"), ("invalid_value", 400, "input[{n}].content"),
    ("ambiguous", 400, "input[{n}].type"),
])
def test_unrelated_errors_and_historical_item_rejections_are_not_retried(monkeypatch, code, status, param):
    from dataclasses import replace

    from agent_py_agent.agent.conversation.auxiliary_model_call import (
        generate_auxiliary_model_response,
    )

    agent, _ = _responses_agent(monkeypatch)
    request = _summary_request(agent)
    old = _with_historical_wire_update(monkeypatch, agent.backend)
    history = deepcopy(request.messages)
    sent, errors = [], []

    def send(call):
        sent.append(deepcopy(call.payload))
        index = len(call.payload["input"]) - 2
        error = (ProviderRequestRejectedError("Unsupported configuration_update input[2].type", status_code=400)
                 if code == "ambiguous" else _rejection(index, code=code, status=status, param=param.format(n=index)))
        errors.append(error)
        raise error

    monkeypatch.setattr(http, "post_json", send)
    with pytest.raises(Exception) as caught:
        # 只验证适配器兼容重放；会话级瞬时错误恢复是另一个已有入口。
        generate_auxiliary_model_response(replace(request, messages=history))
    assert caught.value is errors[0] and len(sent) == 1
    assert agent.backend.supports_reasoning_update_items()
    assert sent[0]["input"][0] == old
    assert history == request.messages


@pytest.mark.parametrize("subscription", [False, True])
@pytest.mark.parametrize("code,notation", [("invalid_value", "input[{n}].type"),
    ("unsupported_type", "input.{n}.type"), ("unsupported_value", "input[{n}].type")])
def test_retry_preserves_prefix_and_removes_only_new_item(monkeypatch, subscription, code, notation):
    from dataclasses import replace

    from agent_py_agent.agent.backends import responses
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    agent, _ = _responses_agent(monkeypatch, top_p=0.8)
    if subscription:
        agent.backend.auth_ref = {"mode": "chatgpt"}
    monkeypatch.setattr(responses, "_session_cache_key", lambda sub: {"prompt_cache_key": "stable-fixture"})
    old = _with_historical_wire_update(monkeypatch, agent.backend)
    sent = []

    def send(call):
        sent.append(deepcopy(call.payload))
        if len(sent) == 1:
            n = len(call.payload["input"]) - 2
            raise _rejection(n, code=code, param=notation.format(n=n))
        return _ok()

    monkeypatch.setattr(http, "post_json", send)
    request = _summary_request(agent)
    history = deepcopy(request.messages)
    before = deepcopy(history)
    tool = {"name": "read_file", "description": "只读", "input_schema": {"type": "object"}}
    generate_bounded_compact_response(replace(request, messages=history, tools=[tool], tool_choice=ToolChoice.auto()))
    assert len(sent) == 2 and history == before
    expected = deepcopy(sent[0])
    del expected["input"][-2]
    expected["reasoning"] = {"effort": "low"}
    assert sent[1] == expected
    assert sent[1]["input"][0] == old
    assert sent[1]["prompt_cache_key"] == "stable-fixture" and sent[1]["top_p"] == 0.8


@pytest.mark.parametrize("scenario", ["remaining", "cancel", "exhausted"])
def test_retry_uses_remaining_deadline_and_honors_cancellation(monkeypatch, scenario):
    from agent_py_agent.agent.backends import ProviderRequestOptions, responses
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    agent, _ = _responses_agent(monkeypatch)
    clock, sent = [100.0], []
    monkeypatch.setattr(responses.time, "monotonic", lambda: clock[0])
    original = agent.backend._send_responses_request

    def send(payload, headers, request):
        sent.append(request.total_deadline_seconds)
        if len(sent) == 1:
            clock[0] += 12 if scenario == "exhausted" else 3
            if scenario == "cancel":
                monkeypatch.setattr(responses, "raise_if_cancelled", lambda: (_ for _ in ()).throw(InterruptedError("cancelled")))
            raise _rejection(len(payload["input"]) - 2)
        return original(payload, headers, request)

    monkeypatch.setattr(agent.backend, "_send_responses_request", send)
    options = ProviderRequestOptions(reasoning_effort="max", reasoning_update_effort="low",
        max_output_tokens=16384, total_deadline_seconds=10)
    def generate():
        return agent.backend.generate(CacheStructuredPrompt("前缀", "摘要"), request_options=options)
    if scenario == "remaining":
        assert generate().text and sent == [10, 7]
    else:
        with pytest.raises(InterruptedError if scenario == "cancel" else ProviderTimeoutError):
            generate()
        assert sent == [10]


def test_failed_fallback_preserves_first_error_context(monkeypatch):
    agent, sent, errors = _reject_first(monkeypatch, fail_second=True)
    with pytest.raises(ProviderRequestRejectedError) as caught:
        generate_bounded_compact_response(_summary_request(agent))
    assert caught.value.status_code == 403 and caught.value.__cause__ is errors[0]
    assert len(sent) == 2


def test_inflight_memo_change_cannot_drop_requested_low(monkeypatch):
    agent, sent = _responses_agent(monkeypatch)
    original = agent.backend._generate

    def race(request):
        assert request.reasoning_update_effort == "low" and request.reasoning_effort == "max"
        with agent.backend._reasoning_update_lock:
            agent.backend._reasoning_update_unsupported = True
        return original(request)

    monkeypatch.setattr(agent.backend, "_generate", race)
    result = generate_bounded_compact_response(_summary_request(agent))
    assert result.text and len(sent) == 1
    assert sent[0]["reasoning"] == {"effort": "low"} and sent[0]["max_output_tokens"] == 16384
    assert not any(item.get("type") == "configuration_update" for item in sent[0]["input"])


def test_fresh_backend_resets_negative_capability(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.backends.factory import get_backend

    agent, sent, _ = _reject_first(monkeypatch)
    generate_bounded_compact_response(_summary_request(agent))
    assert not agent.backend.supports_reasoning_update_items()
    fresh = get_backend(agent.config.model_backend, agent.config)
    assert fresh is not agent.backend and fresh.supports_reasoning_update_items()
    generate_bounded_compact_response(_summary_request(SimpleNamespace(config=agent.config, backend=fresh)))
    assert len(sent) == 3 and sent[-1]["input"][-2]["type"] == "configuration_update"


@pytest.mark.parametrize("event", [
    {"type": "response.output_text.delta", "delta": "已生成正文"},
    {"type": "response.function_call_arguments.delta", "item_id": "call-fixture", "delta": "{"},
])
def test_no_compatibility_retry_after_generation_delta(monkeypatch, event):
    from agent_py_agent.agent.backends import ProviderRequestOptions
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    agent, _ = _responses_agent(monkeypatch, stream_enabled=True)
    events, payloads = [], []

    def stream(path, payload, headers, **options):
        payloads.append(deepcopy(payload))
        try:
            yield json.dumps(event)
            raise _rejection(len(payload["input"]) - 2)
        finally:
            events.append("closed")

    monkeypatch.setattr(agent.backend, "request_stream_iter", stream)
    with pytest.raises(ProviderRequestRejectedError):
        agent.backend.generate(CacheStructuredPrompt("冻结前缀", "摘要"), request_options=ProviderRequestOptions(
            reasoning_effort="max", reasoning_update_effort="low", max_output_tokens=16384))
    assert len(payloads) == 1 and events == ["closed"]
    assert agent.backend.supports_reasoning_update_items()


def test_rejected_stream_is_closed_before_compatibility_retry(monkeypatch):
    from agent_py_agent.agent.backends import ProviderRequestOptions
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    agent, _ = _responses_agent(monkeypatch, stream_enabled=True)
    events = []

    def stream(path, payload, headers, **options):
        attempt = 1 + events.count("start")
        if attempt == 2:
            assert events == ["start", "close-1"]
        events.append("start")
        try:
            if attempt == 1:
                raise _rejection(len(payload["input"]) - 2)
            yield json.dumps({"type": "response.completed", "response": _ok()})
        finally:
            events.append(f"close-{attempt}")

    monkeypatch.setattr(agent.backend, "request_stream_iter", stream)
    response = agent.backend.generate(CacheStructuredPrompt("冻结前缀", "摘要"), request_options=ProviderRequestOptions(
        reasoning_effort="max", reasoning_update_effort="low", max_output_tokens=16384))
    assert response.text and events == ["start", "close-1", "start", "close-2"]


def test_logical_call_has_two_physical_attempts(monkeypatch):
    from agent_py_agent.agent.backends import gateway_helpers

    agent, _ = _responses_agent(monkeypatch)
    opened, observed = [], []

    class Reply(io.BytesIO):
        headers = {}
        status = 200

    def open_request(req, request):
        payload = json.loads(req.data)
        opened.append(payload)
        if len(opened) == 1:
            n = len(payload["input"]) - 2
            body = json.dumps({"error": {"code": "unsupported_type", "param": f"input[{n}].type",
                "message": "fixture rejection"}}).encode()
            raise HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))
        return Reply(json.dumps(_ok()).encode())

    monkeypatch.setattr(http, "post_json", gateway_helpers.post_json)
    monkeypatch.setattr(gateway_helpers, "_gateway_urlopen", open_request)
    response = generate_bounded_compact_response(_summary_request(agent, on_observation=observed.append))
    records = agent._model_call_ledger.records()
    assert response.text and len(opened) == 2 and len(records) == 1
    assert records[0].provider_attempt_count == observed[0].provider_http_attempt_count == 2
    assert observed[0].call_id == records[0].call_id
    assert opened[0]["reasoning"] == {"effort": "max"} and opened[1]["reasoning"] == {"effort": "low"}
