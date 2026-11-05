"""#47：明确格式拒绝只降级一次；离线传输，不代表真实供应商或部署验收。"""
import json
import urllib.error
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import self_map_bootstrap as bootstrap
from agent_py_agent.agent.backends import gateway_helpers as gateway
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.errors import (
    ProviderRequestRejectedError,
    ProviderTimeoutError,
    is_provider_recoverable_error,
)
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.workflows import WorkflowEngine
from agent_py_agent.agent.workflows.models import AnalysisContext

MESSAGE = (
    "Upstream request failed: [invalid_request_error] "
    "This response_format type is unavailable now"
)
SCHEMA = {"type": "object", "additionalProperties": True}
ISOLATION = {"requested": "stateless_skill", "mode": "stateless_skill", "note": ""}


def http_error(error, status=400):
    return urllib.error.HTTPError(
        "https://provider.example.test/v1/chat/completions",
        status, "rejected", {},
        BytesIO(json.dumps({"error": error}).encode()),
    )


def rejection(error=None, status=400):
    return gateway._runtime_http_error(http_error(
        error if error is not None else {
            "type": "invalid_request_error", "message": MESSAGE,
        }, status,
    ))


def host(monkeypatch, outcomes, mode="native"):
    """正式辅助出口及后端组包/解析不替换，只合成传输结果。"""
    backend = OpenAICompatibleBackend(BackendOptions(
        api_base="https://provider.example.test/v1",
        api_key="fixture-key", model_name="fixture-model",
        stream_enabled=False, structured_output=mode,
    ))
    pending, sent = iter(outcomes), []

    def request_json(path, payload, headers, **kwargs):
        sent.append(deepcopy(payload))
        result = next(pending)
        if isinstance(result, BaseException):
            raise result
        return {
            "id": "fixture-response",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(backend, "request_json", request_json)
    agent = SimpleNamespace(
        backend=backend, config=SimpleNamespace(),
        _current_run_params=SimpleNamespace(
            run_id="fixture-run", task_id="fixture-task",
            run_request_id="fixture-request",
            task_attributes={"conversation_thread_id": "fixture-thread"},
        ),
    )
    return agent, sent


def user_text(payload):
    return next(row["content"] for row in payload["messages"] if row["role"] == "user")


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "message": MESSAGE},
    {"param": "response_format", "code": "unsupported_parameter"},
    {"param": "response_format.type", "code": "unsupported_value"},
    {"param": "json_schema", "code": "unsupported_value"},
    {"code": "response_format_unavailable"},
    {"code": "json_schema_not_supported"},
])
def test_response_format_rejection_classified(error):
    exc = rejection(error)
    assert type(exc) is ProviderRequestRejectedError
    assert exc.status_code == 400
    assert exc.error_code == "PROVIDER_REQUEST_REJECTED"
    assert exc.details["rejected_feature"] == "response_format"
    assert "rejection_diagnostic" in exc.details
    assert not is_provider_recoverable_error(exc)


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "message": "bad request"},
    {"param": "messages", "code": "unsupported_parameter"},
    {"param": "response_format", "code": "invalid_json_schema"},
    {"param": "response_format", "code": "invalid_request_error"},
    {"message": "response_format schema unavailable"},
    {"message": "prefix " + MESSAGE},
    {"message": MESSAGE + " suffix"},
    {"message": [MESSAGE]},
    {"param": ["response_format"], "code": "unsupported_value"},
])
def test_other_400_not_classified(error):
    exc = rejection(error)
    assert type(exc) is ProviderRequestRejectedError
    assert "rejected_feature" not in exc.details


@pytest.mark.parametrize("status", [401, 403, 422, 500])
def test_marker_does_not_reclassify_other_http_statuses(status):
    exc = rejection(status=status)
    assert "rejected_feature" not in exc.details


def test_backend_does_not_generically_retry_classified_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway, "_dump_provider_rejection", lambda exc: None)

    def operation():
        calls.append(True)
        raise http_error({"type": "invalid_request_error", "message": MESSAGE})

    with pytest.raises(ProviderRequestRejectedError) as caught:
        gateway._retry_unlabeled_rejection(operation)
    assert calls == [True]
    assert caught.value.details["rejected_feature"] == "response_format"
    assert not gateway._should_retry_http_error(
        http_error({"message": MESSAGE}), 0, 3,
    )


@pytest.mark.parametrize("mode", ["native", "json_object"])
def test_native_and_explicit_json_object_packaging_unchanged(monkeypatch, mode):
    agent, sent = host(monkeypatch, ['{"value":8}'], mode)
    context = AnalysisContext(run_inputs={"n": 4})
    assert bootstrap._workflow_analysis_handler(agent)({}, context, ()) == {"value": 8}
    assert len(sent) == 1
    wire = sent[0]
    if mode == "native":
        assert wire["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "my_agent_structured_output",
                "strict": True, "schema": SCHEMA,
            },
        }
        assert json.loads(user_text(wire))["run_inputs"] == {"n": 4}
    else:
        assert wire["response_format"] == {"type": "json_object"}
        assert "JSON Schema：" + json.dumps(
            SCHEMA, ensure_ascii=False, sort_keys=True,
        ) in user_text(wire)
    assert "tools" not in wire and "tool_choice" not in wire
    assert agent.backend.structured_output == mode
    assert context.analysis_attempts == [{
        "attempt": 1, "response_schema": "native", "outcome": "succeeded",
        "analysis_isolation": ISOLATION,
    }]
    assert context.analysis_isolation == ISOLATION


@pytest.mark.parametrize("mode", ["native", "json_object"])
def test_schema_rejection_downgrades_once_and_keeps_identity(monkeypatch, mode):
    agent, sent = host(monkeypatch, [rejection(), '{"value":8}'], mode)
    context = AnalysisContext(
        prior_outputs={"prefix": {"value": 1}}, run_inputs={"n": 4},
    )
    assert bootstrap._workflow_analysis_handler(agent)({}, context, ()) == {"value": 8}
    assert len(sent) == 2
    assert "response_format" in sent[0] and "response_format" not in sent[1]
    assert all("tools" not in p and "tool_choice" not in p for p in sent)
    assert agent.backend.structured_output == mode
    if mode == "native":
        assert sent[0]["messages"] == sent[1]["messages"]
    first, second = context.analysis_attempts
    assert first == {
        "attempt": 1, "response_schema": "native", "outcome": "failed",
        "error_type": "ProviderRequestRejectedError",
        "error_code": "PROVIDER_REQUEST_REJECTED",
        "rejected_feature": "response_format",
        "analysis_isolation": ISOLATION,
    }
    assert second == {
        "attempt": 2, "response_schema": None, "outcome": "succeeded",
        "analysis_isolation": ISOLATION,
    }
    assert context.analysis_isolation == ISOLATION
    assert first["analysis_isolation"] is not second["analysis_isolation"]


def test_non_schema_400_is_not_replayed(monkeypatch):
    error = rejection({"type": "invalid_request_error", "message": "bad messages"})
    agent, sent = host(monkeypatch, [error])
    context = AnalysisContext()
    with pytest.raises(ProviderRequestRejectedError) as caught:
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert caught.value is error and len(sent) == 1
    assert len(context.analysis_attempts) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_unknown_execution_outcome_is_not_replayed(monkeypatch, fallback):
    error = RequestExecutionOutcomeUncertainError({
        "id": "fixture-request", "execution_attempt_id": "fixture-attempt",
        "lease_epoch": 1, "lease_owner": "fixture-holder",
    }, "fixture")
    outcomes = [rejection(), error] if fallback else [error]
    agent, sent = host(monkeypatch, outcomes)
    context = AnalysisContext()
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert caught.value is error
    assert len(sent) == (2 if fallback else 1)
    assert context.analysis_attempts[-1]["error_code"] == "ACTIVE_TURN_OUTCOME_UNCERTAIN"


def test_timeout_is_not_schema_evidence(monkeypatch):
    error = ProviderTimeoutError("fixture", stage="wall_clock")
    agent, sent = host(monkeypatch, [error])
    with pytest.raises(ProviderTimeoutError) as caught:
        bootstrap._workflow_analysis_handler(agent)({}, AnalysisContext(), ())
    assert caught.value is error and len(sent) == 1


@pytest.mark.parametrize("text", ["not json", "[]", "null", '"string"', '{"value":NaN}'])
def test_fallback_still_rejects_non_json_or_wrong_type(monkeypatch, text):
    agent, sent = host(monkeypatch, [rejection(), text])
    context = AnalysisContext()
    with pytest.raises(ValueError):
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert len(sent) == 2
    assert [a["outcome"] for a in context.analysis_attempts] == ["failed", "failed"]


def test_both_rejections_keep_both_attempts(monkeypatch):
    second = rejection()
    agent, sent = host(monkeypatch, [rejection(), second])
    context = AnalysisContext()
    with pytest.raises(ProviderRequestRejectedError) as caught:
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert caught.value is second and len(sent) == 2
    assert [a["response_schema"] for a in context.analysis_attempts] == ["native", None]
    assert all(a["outcome"] == "failed" for a in context.analysis_attempts)
    assert all(a["rejected_feature"] == "response_format" for a in context.analysis_attempts)


@pytest.mark.parametrize("response", [
    SimpleNamespace(text="{}", tool_use_blocks=[{}]),
    SimpleNamespace(text="{}", tool_protocol_violations=[{}]),
    SimpleNamespace(text="{}", truncated=True),
    SimpleNamespace(text="{}", runtime_status="unknown"),
])
@pytest.mark.parametrize("fallback", [False, True])
def test_tool_incomplete_and_unknown_responses_fail_without_replay(
    monkeypatch, response, fallback,
):
    agent, _ = host(monkeypatch, [])
    calls = []

    def generate(request):
        calls.append(request)
        assert request.tools is None and request.tool_choice is None
        if fallback and len(calls) == 1:
            raise rejection()
        return response

    monkeypatch.setattr(bootstrap, "generate_auxiliary_model_response", generate)
    context = AnalysisContext()
    with pytest.raises(ValueError):
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert len(calls) == (2 if fallback else 1)
    assert context.analysis_attempts[-1]["outcome"] == "failed"


def test_cancel_between_attempts_prevents_second_call(monkeypatch):
    agent, _ = host(monkeypatch, [])
    token, calls, context = CancellationToken(), [], AnalysisContext()

    def generate(request):
        calls.append(request)
        token.cancel("fixture-stop")
        raise rejection()

    monkeypatch.setattr(bootstrap, "generate_auxiliary_model_response", generate)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled):
        bootstrap._workflow_analysis_handler(agent)({}, context, ())
    assert len(calls) == 1
    assert len(context.analysis_attempts) == 1
    assert context.analysis_attempts[0]["rejected_feature"] == "response_format"


def test_interrupt_between_attempts_prevents_second_call(monkeypatch):
    agent, _ = host(monkeypatch, [])
    state, calls = {"stopped": False}, []
    monkeypatch.setattr(bootstrap, "is_interrupted", lambda: state["stopped"])

    def generate(request):
        calls.append(request)
        state["stopped"] = True
        raise rejection()

    monkeypatch.setattr(bootstrap, "generate_auxiliary_model_response", generate)
    with pytest.raises(InterruptedError):
        bootstrap._workflow_analysis_handler(agent)({}, AnalysisContext(), ())
    assert len(calls) == 1


@pytest.mark.parametrize("tail", ['{"value":8}', "not json", "[]", None])
def test_engine_preserves_prefix_and_persists_attempts(tmp_path, monkeypatch, tail):
    """真实两步引擎：第二步内部降级不重新执行成功前缀，双失败也落账。"""
    ending = rejection() if tail is None else tail
    agent, sent = host(monkeypatch, ['{"value":1}', rejection(), ending])
    definition = {
        "descriptor": {
            "id": "schema-flow", "name": "schema-flow", "kind": "workflow",
            "access_targets": [], "data_sources": [],
            "credential_refs": [], "io_contract": {},
        },
        "steps": [
            {"step_id": name, "name": name, "mode": "llm_skill_analysis"}
            for name in ("prefix", "tail")
        ],
    }
    engine = WorkflowEngine(
        tmp_path, analysis_handler=bootstrap._workflow_analysis_handler(agent),
    )
    run = engine.run_workflow(definition)
    expected = "succeeded" if tail == '{"value":8}' else "failed"
    assert run.status == expected
    assert len(sent) == 3
    assert [json.loads(user_text(p))["step"]["step_id"] for p in sent] == [
        "prefix", "tail", "tail",
    ]
    prefix, last = run.steps
    assert prefix.status == "succeeded" and prefix.output_snapshot == {"value": 1}
    assert last.status == expected
    attempts = last.escalation_result["analysis_attempts"]
    assert [a["response_schema"] for a in attempts] == ["native", None]
    assert attempts[0]["error_code"] == "PROVIDER_REQUEST_REJECTED"
    assert attempts[0]["rejected_feature"] == "response_format"
    assert attempts[1]["outcome"] == expected
    persisted = engine.store.load_run(run.workflow_run_id)
    assert persisted.steps[0].to_dict() == prefix.to_dict()
    assert persisted.steps[1].escalation_result["analysis_attempts"] == attempts
