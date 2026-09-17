import copy
import json

from agent_py_agent.agent.backends.cache_diagnostics import (
    compare_request_surfaces,
    public_cache_diagnostic,
    request_surface,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallLedger,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
)


def test_diagnostics_do_not_modify_payload_or_keep_plaintext():
    payload = {"model": "sample", "system": "private system", "messages": [{"role": "user", "content": "private user"}],
               "tools": [{"name": "tool_a"}], "secret_extension": "secret-value"}
    original = copy.deepcopy(payload)
    surface = request_surface(payload, "https://private.example/v1")
    assert payload == original
    assert "private" not in json.dumps(surface)
    assert "secret-value" not in json.dumps(surface)
    assert compare_request_surfaces({}, surface)["baseline_available"] is False


def test_append_and_compact_are_different_diagnostics():
    payload = {"model": "a", "messages": [{"role": "user", "content": "one"}]}
    first = request_surface(payload, "endpoint")
    payload["messages"].append({"role": "assistant", "content": "two"})
    second = request_surface(payload, "endpoint")
    assert compare_request_surfaces(first, second)["changes"] == ["history_appended"]
    assert compare_request_surfaces(second, first)["changes"] == ["history_shortened"]
    payload["messages"][0]["content"] = "new summary"
    assert "history_prefix_changed" in compare_request_surfaces(second, request_surface(payload, "endpoint"))["changes"]
    payload["tools"] = [{"name": "different"}]
    assert "tools_changed" in compare_request_surfaces(second, request_surface(payload, "endpoint"))["changes"]


def test_large_history_is_explicitly_partial():
    surface = request_surface({"messages": [{"content": str(i)} for i in range(600)]}, "endpoint")
    assert len(surface["messages"]) == 512
    result = compare_request_surfaces(surface, surface)
    assert result["partial"] is True
    assert result["server_cache_state"] == "unknown"


def test_ledger_does_not_compare_different_threads():
    ledger = ModelCallLedger()
    surface = request_surface({"model": "a", "messages": []}, "endpoint")
    for call, thread in [("a", "thread-a"), ("b", "thread-b"), ("c", "thread-a")]:
        ledger.started(ModelCallStartedParams(call, "test", "a", 0, run_id=call, metadata={"thread_id": thread}))
        ledger.provider_attempt(ModelCallProviderAttemptParams(call, call, "started", request_surface=surface))
    rows = ledger.records()
    assert rows[1].metadata["cache_diagnostic"]["baseline_available"] is False
    assert rows[2].metadata["cache_diagnostic"]["changes"] == []
    assert rows[2].metadata["cache_diagnostic"]["server_cache_state"] == "unknown"


def test_public_diagnostic_never_persists_raw_values_or_server_guesses():
    from agent_py_agent.agent.conversation.model_metrics import public_model_metrics

    value = {"baseline_available": True, "changes": ["private prompt", "tools_changed", {}],
             "shared_message_prefix": 9999, "server_cache_state": "hit", "private": "secret"}
    result = public_model_metrics({"schema": "model_runtime_metrics.v1", "cache_diagnostic": value})
    assert result["cache_diagnostic"] == public_cache_diagnostic(value)
    assert result["cache_diagnostic"]["changes"] == ["tools_changed"]
    assert result["cache_diagnostic"]["shared_message_prefix"] == 512
    assert result["cache_diagnostic"]["server_cache_state"] == "unknown"
    assert "private" not in json.dumps(result)
    assert "cache_diagnostic" not in public_model_metrics({"schema": "model_runtime_metrics.v1"})
