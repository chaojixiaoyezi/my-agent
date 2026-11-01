"""W9: provider detail travels through the real classifier and public reports."""
import json

import pytest

from agent_py_agent.agent.backends.errors import (
    ProviderRequestRejectedError,
    ProviderTransientError,
    ProviderUsageLimitError,
    provider_recoverable_report,
)
from agent_py_agent.agent.backends.responses_failure import failed_event_error
from agent_py_agent.agent.contracts.diagnostics import DIAGNOSTIC_DETAILS_MAX_BYTES
from agent_py_agent.agent.gateway_parts.request_errors import (
    gateway_provider_error_projection,
    provider_rejection_user_notice,
)
from agent_py_agent.agent.runtime_errors import runtime_error_report


@pytest.mark.parametrize("code,kind", [("rate_limit_exceeded", ProviderUsageLimitError),
                                        ("server_error", ProviderTransientError),
                                        ("websocket_connection_limit_reached", ProviderTransientError)])
@pytest.mark.parametrize("wire", ["error", "response.failed"])
def test_stream_failure_keeps_typed_details_and_recovery_route(code, kind, wire):
    raw = {"code": code, "type": "capacity", "param": "connections", "message": "pool north busy",
           "request_id": "request-one"}
    event = {"type": wire, "error": raw} if wire == "error" else {"type": wire, "response": {"error": raw}}
    error = failed_event_error(event)
    assert type(error) is kind
    assert (getattr(error, "details", None) or {}).get("provider_error") == raw


@pytest.mark.parametrize("reason,param", [("north pool busy", "connections"), ("south pool busy", "requests")])
def test_public_reports_keep_different_safe_reasons(reason, param):
    error = failed_event_error({"type": "error", "error": {
        "code": "rate_limit_exceeded", "type": "capacity", "param": param, "message": reason,
        "request_id": "request-one"}})
    report = runtime_error_report(error)
    public = gateway_provider_error_projection(error)
    expected = {"code": "rate_limit_exceeded", "type": "capacity", "param": param,
                "message": reason, "request_id": "request-one"}
    assert report.get("provider_error") == expected
    assert public.get("provider_error") == expected
    assert public.get("diagnostic") == report.get("diagnostic")
    diagnostic = public.get("diagnostic") or {}
    assert diagnostic.get("provider_error") == {"provider_code": "rate_limit_exceeded", "request_id": "request-one"}
    assert diagnostic.get("code") == "TRANSIENT_ERROR"
    assert diagnostic.get("retryable") is True
    notice = provider_rejection_user_notice("模型暂时不可用", public)
    assert reason in notice and param in notice


def test_public_projection_redacts_before_clipping_and_drops_opaque_fields():
    error = ProviderTransientError("temporary", details={"provider_error": {
        "code": "server_error", "type": "capacity", "param": "input",
        "message": "Bearer secret-bearer-value password=private-password " + "界" * 20000,
        "token": "private-token", "prompt": "private-prompt", "object": object()}})
    report = runtime_error_report(error)
    public = gateway_provider_error_projection(error)
    encoded = json.dumps(public, ensure_ascii=False)
    assert public.get("provider_error"), "provider details must not disappear"
    for secret in ("secret-bearer-value", "private-password", "private-token", "private-prompt"):
        assert secret not in encoded
    assert len(encoded.encode("utf-8")) <= DIAGNOSTIC_DETAILS_MAX_BYTES
    assert report.get("details_truncated") is True
    assert public.get("details_truncated") is True


def test_unknown_code_does_not_become_transient_and_http_diagnostic_survives():
    unknown = failed_event_error({"type": "error", "error": {"code": "novel_reason", "message": "not temporary"}})
    public = gateway_provider_error_projection(unknown)
    assert public.get("provider_error", {}).get("code") == "novel_reason"
    assert public.get("diagnostic", {}).get("retryable") is False
    assert unknown.error_code == "MODEL_RESPONSE_FAILED"
    http = ProviderRequestRejectedError("rejected", status_code=422, details={"rejection_diagnostic": {
        "status_code": 422, "headers": {"x-request-id": "http-one"}, "body_excerpt": "invalid field"}})
    projected = gateway_provider_error_projection(http)
    assert projected["http_status"] == 422
    assert projected["rejection_diagnostic"]["headers"]["x-request-id"] == "http-one"


def test_stream_truncation_is_explicit_at_the_original_exception_and_public_boundary():
    error = failed_event_error({'type': 'error', 'error': {'code': 'server_error', 'message': 'x' * 6000}})
    assert error.details.get('details_truncated') is True
    assert gateway_provider_error_projection(error).get('details_truncated') is True


def test_original_cli_report_can_distinguish_safe_stream_causes():
    error = failed_event_error({'type': 'error', 'error': {'code': 'server_error',
        'type': 'capacity', 'param': 'connections', 'message': 'north pool busy', 'request_id': 'request-cli'}})
    notice = provider_recoverable_report(error)
    assert 'north pool busy' in notice and 'connections' in notice
    assert 'request-cli' in notice


def test_unknown_numeric_provider_code_keeps_its_previous_scalar_representation():
    error = failed_event_error({'type': 'error', 'error': {'code': 12345, 'message': 'unknown failure'}})
    assert error.details['provider_error'].get('code') == '12345'
    assert error.error_code == 'MODEL_RESPONSE_FAILED'
