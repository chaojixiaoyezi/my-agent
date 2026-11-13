"""规范模型累计delta经真实BusinessClient/HTTP投影完整provider用量。"""

import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    model_call_summary,
    record_model_call_finished,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger, ModelCallStartedParams
from agent_py_agent.tests import test_v13_w0_observation_readers as original
from agent_py_agent.tests.test_acceptance_observation_projections import (
    background_http,
    client_for,
    isolated_home,
    sampled_owner,
)

_FIXTURES = (background_http, isolated_home, sampled_owner)


def _settle(ledger, task, number, *, with_details=True, estimated=False):
    call = f"provider-call-{number}"
    ledger.started(
        ModelCallStartedParams(
            call,
            "offline-provider",
            "offline-model",
            17,
            request_id="wire-model-request",
            run_id="observed-" + task.task_id,
            metadata={"purpose": "main"},
        )
    )
    usage = None if estimated else {"input_tokens": number * 100}
    if usage is not None and with_details:
        usage.update(output_tokens=number, input_tokens_details={"cached_tokens": number * 80})
    record_model_call_finished(
        ledger,
        call,
        ModelResponse(text="offline response", backend="offline-provider", usage=usage),
    )


@pytest.mark.parametrize("details", [True, False])
def test_native_provider_usage_sums_cumulative_deltas_once_and_preserves_missing_fields(
    background_http, sampled_owner, details
):  # noqa: F811
    services, task, runtime, _run = sampled_owner
    ledger = ModelCallLedger()
    model = SimpleNamespace(_model_call_ledger=ledger)
    store = services.host.conversation_store.model_usage
    for number in (1, 2):
        _settle(ledger, task, number, with_details=details)
        snapshot = model_call_summary(
            model, request_id="wire-model-request", run_id="observed-" + task.task_id
        )
        request = {
            "thread_id": task.target_thread_id,
            "request_id": "wire-model-request",
            "run_id": "observed-" + task.task_id,
            "task_id": task.task_id,
            "source": "main",
            "model_calls": snapshot,
            "now": time.time(),
        }
        store.append_snapshot_once(request)
        store.append_snapshot_once(request)
    _settle(ledger, task, 3, estimated=True)
    snapshot = model_call_summary(
        model, request_id="wire-model-request", run_id="observed-" + task.task_id
    )
    store.append_snapshot_once({**request, "model_calls": snapshot})
    expected = store.summary(task.target_thread_id)
    assert expected["provider"]["input_tokens"] == 300
    original.wire(services, runtime)
    response = client_for(background_http).call("usage.get", {"filter": {"task_id": task.task_id}})
    assert response["ok"], response
    data = response["data"]
    assert data["actual"]["input_tokens"] == expected["provider"]["input_tokens"]
    assert data["actual"]["output_tokens"] == (3 if details else None)
    assert data["actual"]["cached_input_tokens"] == (240 if details else None)
    assert data["actual"]["uncached_input_tokens"] == (60 if details else None)
    assert data["estimated"]["input_tokens"] == expected["estimated"]["input_tokens"]
    assert data["estimated"]["input_tokens"] != data["actual"]["input_tokens"]
    assert data["actual"]["cost"] is data["actual"]["currency"] is None
    assert "decision_usage_only" not in data["unknown"]
    # 同一wire请求不改原账或累加第二次。
    assert (
        client_for(background_http).call("usage.get", {"filter": {"task_id": task.task_id}})["data"]
        == data
    )


def test_uncached_requires_fields_from_every_provider_call_not_matching_subset_counts():
    from agent_py_agent.agent.gateway_parts.business_api_v13_readers import _model_usage_measures

    ledger = ModelCallLedger()
    model = SimpleNamespace(_model_call_ledger=ledger)
    for index, usage in enumerate(
        [{"input_tokens": 100}, {"input_tokens_details": {"cached_tokens": 80}}]
    ):
        call = f"partial-{index}"
        ledger.started(ModelCallStartedParams(call, "offline", "model", 17, request_id="request"))
        record_model_call_finished(
            ledger, call, ModelResponse(text="response", backend="offline", usage=usage)
        )
    summary = model_call_summary(model, request_id="request")
    provider = summary["usage_breakdown"]["provider"]
    assert (
        provider["input_tokens_reported_call_count"]
        == provider["cache_read_input_tokens_reported_call_count"]
        == 1
    )
    assert provider["call_count"] == 2
    actual, _ = _model_usage_measures(
        summary, {"input_reported_calls": 0, "estimated_unfinished_calls": 0}
    )
    assert actual["input_tokens"] == 100 and actual["cached_input_tokens"] == 80
    assert actual["uncached_input_tokens"] is None
