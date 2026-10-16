from __future__ import annotations

import contextvars
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import request_scope as scope
from agent_py_agent.agent.backends.provider_headers import provider_runtime_scope
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter


def test_provider_owner_comes_from_host_and_survives_context_copy():
    home = SimpleNamespace(owner_provider="im", owner_kind="user", owner_id="A")
    agent = SimpleNamespace(home_paths=home)
    params = SimpleNamespace(task_attributes={}, thread_id="first")
    with provider_runtime_scope(agent, params):
        first = scope._OWNER.get()
        copied = contextvars.copy_context().run(scope._OWNER.get)
    params.thread_id = "second"
    with provider_runtime_scope(agent, params):
        assert scope._OWNER.get() == first
    assert first and copied == first and "A" not in first
    assert scope._OWNER.get() == ""


def test_queue_event_reaches_tui_stream_without_retry_or_private_identity(tmp_path):
    from agent_py_agent.agent.agent_core.tool_model_generation import _publish_transport_retry

    path = tmp_path / "chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)
    value = {"phase": "waiting", "wait_seconds": 2.1, "admission_wait_reason": "provider_concurrency",
             "owner": "private-owner", "endpoint": "private-endpoint", "api_key": "private-key"}
    _publish_transport_retry(writer, {"status": "queued", "provider_queue": value})
    assert path.exists()
    event = json.loads(path.read_text().splitlines()[0])
    assert event["kind"] == "runtime_progress"
    assert "排队中" in event["text"]
    assert event["provider_queue"] == {"phase": "waiting", "wait_seconds": 2.1,
                                      "admission_wait_reason": "provider_concurrency"}
    assert "retry" not in event and "private-" not in path.read_text()
    assert event["verbose_level"] == "full"
    # 使用真实 TUI 事件适配与存储，不把 chunk 写入当成界面已显示。
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("queue-session")
    runtime.enqueue_prompt("queue-request", "continue", queued=False)
    turn = runtime.begin_turn("queue-request")
    assert turn.on_gateway_event(event)
    assert any("排队中" in block.text for block in runtime.store.snapshot().stable_blocks)
    from agent_py_agent.cli.chat_parts.tui_block_renderer import (
        TuiRenderContext,
        render_tui_snapshot,
    )

    for detailed in (False, True):
        frame = render_tui_snapshot(runtime.store.snapshot(), TuiRenderContext(width=100, detailed_transcript=detailed))
        text = "\n".join("".join(fragment[1] for fragment in line) for line in frame.transcript_lines)
        assert "排队中" in text


def test_queue_timeout_contract_is_transient_in_gateway():
    from agent_py_agent.agent.backends.errors import ProviderQueueTimeoutError
    from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
    from agent_py_agent.agent.runtime_errors import fallback_error_code

    exc = ProviderQueueTimeoutError("queued")
    assert fallback_error_code(exc) == "PROVIDER_QUEUE_TIMEOUT"
    contract = ERROR_CONTRACTS[exc.error_code]
    assert contract.retryable and contract.category == "transient"


def test_queue_timeout_uses_real_retry_gateway_and_background_classifiers():
    from agent_py_agent.agent.agent_core.provider_transient_auto_resume import (
        _raise_unless_provider_transient,
    )
    from agent_py_agent.agent.backends.errors import ProviderQueueTimeoutError
    from agent_py_agent.agent.conversation.background_claim import _counts_as_policy_failure
    from agent_py_agent.agent.conversation.background_supply_backoff import (
        ProviderSupplyBackoff,
        consume_with_supply_guard,
    )
    from agent_py_agent.agent.runtime_errors import runtime_error_report

    exc = ProviderQueueTimeoutError("quota configuration billing are fixture words, not authority")
    _raise_unless_provider_transient(exc)
    gateway = runtime_error_report(exc, context="gateway_request.execute")
    assert gateway["category"] == "provider_transient" and gateway["recoverable"]
    assert not _counts_as_policy_failure(exc)
    backoff = ProviderSupplyBackoff()

    def run():
        raise exc

    assert consume_with_supply_guard(backoff, "thread-A", 100, run) is None
    assert not backoff.should_attempt("thread-A", 101)
    assert backoff.should_attempt("thread-B", 101)


@pytest.mark.parametrize("purpose", ["main", "compact", "decision", "probe:tool_capability"])
def test_queue_events_never_create_ledger_attempts_for_any_call_purpose(purpose):
    from agent_py_agent.agent.agent_core.model.call_runtime import record_model_provider_attempt
    from agent_py_agent.agent.contracts.model_call_ledger import (
        ModelCallLedger,
        ModelCallStartedParams,
    )

    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="queued-call", backend="fake", model="fake", input_tokens=12,
                                         metadata={"purpose": purpose}, is_probe=purpose.startswith("probe:")))
    before = ledger.records()[0]
    for phase in ("waiting", "admitted", "cancelled"):
        record_model_provider_attempt(ledger, "queued-call", {"status": "queued", "provider_queue": {
            "phase": phase, "admission_wait_reason": "provider_concurrency", "wait_seconds": 3}})
    assert ledger.records()[0] == before
    record_model_provider_attempt(ledger, "queued-call", {"status": "started", "attempt_id": "physical-http"})
    assert ledger.records()[0].provider_attempt_count == 1


def test_closed_writer_rejects_late_queue_events(tmp_path):
    path = tmp_path / "chunks.jsonl"
    writer = BufferedChunkStreamWriter(path, rich_transcript=True)
    writer.write_provider_queue({"phase": "waiting", "wait_seconds": 3})
    writer.close()
    before = path.read_bytes()
    writer.write_provider_queue({"phase": "admitted", "wait_seconds": 4})
    assert path.read_bytes() == before


def test_queue_phase_reaches_only_bound_main_activity_and_preserves_approval_wait(tmp_path):
    from agent_py_agent.agent.conversation.agent_activity import background_main_activity
    from agent_py_agent.agent.gateway_parts.main_activity import GatewayMainActivitySink

    agent = SimpleNamespace()
    sink = GatewayMainActivitySink(agent, thread_id="thread-a", request_id="request-a", request={}, task_id="task-a")
    writer = BufferedChunkStreamWriter(tmp_path / "chunks.jsonl", main_activity_sink=sink)
    writer.write_provider_queue({"phase": "waiting", "wait_seconds": 3, "owner": "private-owner"})
    activity = background_main_activity(agent, "thread-a", {"task-a"})
    assert activity["phase"] == "running" and activity["activity"] == "模型接口排队中"
    assert not background_main_activity(agent, "thread-b", {"task-a"})
    assert "private-owner" not in str(activity)
    sink({"kind": "permission_requested", "permission": {"permission_id": "pending-approval"}})
    writer.write_provider_queue({"phase": "admitted", "wait_seconds": 4})
    assert background_main_activity(agent, "thread-a", {"task-a"})["phase"] == "waiting_permission"


def test_background_queue_updates_existing_main_and_im_pulse():
    from agent_py_agent.agent.conversation.agent_activity import (
        BackgroundMainActivitySink,
        background_main_activity,
    )

    agent = SimpleNamespace()
    sink = BackgroundMainActivitySink(agent, thread_id="thread-a", task_id="task-a")
    phases = []
    sink._transcript._im_progress = SimpleNamespace(update=phases.append)
    assert sink.write_provider_queue({"phase": "waiting", "wait_seconds": 3})
    assert phases == ["模型接口排队中"]
    assert background_main_activity(agent, "thread-a", {"task-a"})["activity"] == "模型接口排队中"
    assert not sink.write_provider_queue({"phase": "unknown", "text": "private"})
    assert phases == ["模型接口排队中"]
