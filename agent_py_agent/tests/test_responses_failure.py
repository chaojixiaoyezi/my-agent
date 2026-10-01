"""Responses 流里的 error / response.failed 事件按服务商错误码分类：上下文超限走压缩恢复、额度/限流/临时故障走各自路线，
认不出的保留原错误码；SSE 与 WebSocket 共用 collect_response，网络全部是假连接。"""
import json

import pytest

from agent_py_agent.agent.agent_core.model.context_pressure import is_context_window_error
from agent_py_agent.agent.backends import BackendOptions
from agent_py_agent.agent.backends import responses_websocket as ws
from agent_py_agent.agent.backends.errors import (
    ProviderContextWindowError,
    ProviderQuotaExhaustedError,
    ProviderResponseError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
from agent_py_agent.agent.backends.responses_failure import (
    FAILED_EVENT_ERROR_CODE,
    failed_event_error,
)
from agent_py_agent.agent.backends.responses_wire import collect_response
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.tests.test_responses_websocket import FakeConnection, request


def failed(code, message="", **extra):
    error = {key: value for key, value in (("code", code), ("message", message)) if value}
    return {"type": "response.failed", "response": {"status": "failed", "error": {**error, **extra}}}


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (failed("context_length_exceeded", "Your input exceeds the context window of this model."), ProviderContextWindowError),
        ({"type": "error", "code": "context_length_exceeded", "message": "too long"}, ProviderContextWindowError),
        (failed("insufficient_quota", "You exceeded your current quota."), ProviderQuotaExhaustedError),
        (failed("usage_limit_reached", "The usage limit has been reached"), ProviderQuotaExhaustedError),
        (failed("rate_limit_exceeded", "Rate limit reached. Please try again in 2s."), ProviderUsageLimitError),
        ({"type": "error", "error": {"code": "server_error", "message": "An error occurred"}}, ProviderTransientError),
    ],
)
def test_known_failure_codes_choose_the_existing_recovery_route(event, expected):
    error = failed_event_error(event)
    assert type(error) is expected
    assert "错误码" in str(error)


def test_unknown_or_missing_codes_keep_the_provider_reason():
    unknown = failed_event_error(failed("invalid_prompt", "x" * 900, param="input"))
    assert type(unknown) is ProviderResponseError and unknown.error_code == FAILED_EVENT_ERROR_CODE
    assert "invalid_prompt" in str(unknown)
    assert unknown.details["provider_error"]["code"] == "invalid_prompt"
    assert unknown.details["provider_error"]["param"] == "input"
    assert len(unknown.details["provider_error"]["message"]) == 500
    bare = failed_event_error({"type": "response.failed"})
    assert type(bare) is ProviderResponseError and bare.error_code == FAILED_EVENT_ERROR_CODE
    assert "没有给出错误码" in str(bare) and bare.details["provider_error"] == {}
    assert error_contract(FAILED_EVENT_ERROR_CODE).code == FAILED_EVENT_ERROR_CODE


def test_websocket_context_overflow_reaches_the_main_loop_compaction_predicate(monkeypatch):
    events = [json.dumps({"type": "response.created", "response": {"status": "in_progress"}}),
              json.dumps(failed("context_length_exceeded", "Your input exceeds the context window of this model."))]
    connection = FakeConnection(events)
    monkeypatch.setattr(ws, "_connect", lambda req: connection)
    lines = ws.iter_responses_websocket(request())
    with pytest.raises(ProviderContextWindowError) as caught:
        collect_response(lines, None, None)
    lines.close()
    assert is_context_window_error(caught.value)  # 主循环据此走 context_pressure_response 压缩恢复
    assert caught.value.error_code == "MODEL_CONTEXT_WINDOW_EXCEEDED" and connection.closed


def test_sse_backend_raises_transient_for_server_errors(monkeypatch):
    backend = OpenAIResponsesBackend(BackendOptions("https://example.test/v1", "secret", "model", stream_enabled=True))

    def stream(path, payload, headers, **kwargs):
        return (json.dumps(event) for event in [failed("server_error", "The server had an error while processing your request.")])

    monkeypatch.setattr(backend, "request_stream_iter", stream)
    with pytest.raises(ProviderTransientError):
        backend.generate("你好")
