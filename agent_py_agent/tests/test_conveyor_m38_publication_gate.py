from __future__ import annotations

import subprocess
import sys
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation import background_execution
from agent_py_agent.agent.conversation.background_delivery import (
    BackgroundDeliveryDependencies,
    cached_owner_delivery,
    record_background_response,
)
from agent_py_agent.agent.conversation.background_publication_gate import (
    PublicationWrite,
    publication_gate_for,
)
from agent_py_agent.agent.conversation.background_transcript import (
    read_background_transcript_events,
)
from agent_py_agent.agent.conversation.channels import DeliveryContext, FakeDeliveryService
from agent_py_agent.agent.conversation.display_archive import read_display_archive_page
from agent_py_agent.agent.conversation.history_display import conversation_history_display_events
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import display_archive_service
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.tooling.host_output_redaction import (
    TurnCredentialRedaction,
    turn_credential_redaction,
)


def _case(tmp_path, *, scoped=True):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "display-owner", "channel": "tui",
        "channel_conversation_id": "display-session", "channel_user_id": "display-owner",
    })
    agent = SimpleNamespace(conversation_store=store)
    wake = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": "display-task",
        "reason": "scheduled_progress_report", "metadata": {"input_provider": "credential"},
    }) if scoped else None
    request = BackgroundRunRequest(
        thread_id=thread.thread_id, task_id="display-task",
        wake_signal=wake.to_dict() if wake is not None else None,
    )
    return agent, store, thread, request


def _invoke(agent, store, thread, request):
    return background_execution.invoke_background_turn(
        background_execution.BackgroundExecutionDependencies(agent, store, None),
        thread, request, user_prompt="合成显示回合", continuation_injection=[],
        proactive_delivery_available=False,
    )


def _bytes_containing(root, marker):
    return sorted(
        str(path.relative_to(root)) for path in root.rglob("*")
        if path.is_file() and marker in path.read_bytes()
    )


def test_missing_binding_withholds_all_display_writes(tmp_path, monkeypatch):
    agent, store, thread, request = _case(tmp_path)
    marker = "synthetic-display-only-76395"

    def run(*_args, activity_sink, **_kwargs):
        activity_sink.write_thinking((marker + "\n") * 600)
        background_execution._persist_background_native_turn(store, request, SimpleNamespace(internal_response=marker))
        return SimpleNamespace(response=marker, archive_tool_calls=[{}])

    monkeypatch.setattr(background_execution, "run_background_turn_with_compact", run)
    result, snapshot = _invoke(agent, store, thread, request)
    commit = record_background_response(
        BackgroundDeliveryDependencies(agent, store, FakeDeliveryService(), lambda _: "completed"),
        request, DeliveryContext(channel="feishu", target="test-owner", thread_id=thread.thread_id),
        result.response, display_snapshot=snapshot, tool_call_count=1, deliver=True,
        delivery_reason=request.reason,
    )
    contaminated = _bytes_containing(tmp_path, marker.encode())
    assert contaminated == []
    assert commit.persisted and marker not in commit.content
    replay = conversation_history_display_events(store.messages.recent(thread.thread_id, limit=50))
    assert marker not in repr(replay)
    assert any(event["payload"].get("publication_decision") == "withheld" for event in replay)
    signal = store.wakes.pending_one(request.wake_signal["wake_signal_id"])
    frozen = cached_owner_delivery(signal)
    assert frozen["message_metadata"]["publication_decision"] == "withheld"
    assert marker not in repr(frozen)
    event = next(item for item in read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
                 if item["payload"].get("publication_decision") == "withheld")
    monkeypatch.setattr(display_archive_service, "resolve_gateway_scope_agent", lambda *_: agent)
    archived = display_archive_service.read_gateway_display_page(
        agent, scope=GatewayControlScope("display-owner", "tui", "display-session"),
        reference=event["payload"]["display_archive_ref"], page_index=0,
    )
    assert archived["publication_decision"] == "withheld"
    assert all(row["kind"] == "withheld" for row in archived["rows"])


def test_pending_turn_interruption_keeps_display_private(tmp_path, monkeypatch):
    agent, store, thread, request = _case(tmp_path)
    marker = "synthetic-uncommitted-display-91643"

    def run(*_args, activity_sink, **_kwargs):
        activity_sink.write_thinking((marker + "\n") * 600)
        activity_sink.write_model(marker)
        activity_sink.write_progress({"tool": "read_file", "phase": "started", "round": 1, "call_index": 1})
        activity_sink.archive_tool_rows(iter(({"kind": "text", "text": marker},)))
        assert not _bytes_containing(tmp_path, marker.encode())
        assert marker not in repr(read_background_transcript_events(agent, thread_id=thread.thread_id, after=0))
        raise KeyboardInterrupt

    monkeypatch.setattr(background_execution, "run_background_turn_with_compact", run)
    with pytest.raises(KeyboardInterrupt):
        _invoke(agent, store, thread, request)
    assert not _bytes_containing(tmp_path, marker.encode())
    assert not hasattr(request, "_background_publication_sink")


@pytest.mark.parametrize("binding", ["ordinary", "writer", "registry"])
def test_verified_display_releases_history_and_full_archive(tmp_path, monkeypatch, binding):
    scoped = binding != "ordinary"
    agent, store, thread, request = _case(tmp_path, scoped=scoped)
    marker = "synthetic-display-value-46733"
    public = "example public display"
    redaction = None

    def run(*_args, activity_sink, **_kwargs):
        nonlocal redaction
        if binding == "writer":
            redaction = turn_credential_redaction(agent, RunParams(
                request_id=request.conversation_turn_id, task_id=request.task_id, on_chunk=activity_sink,
            ))
        elif binding == "registry":
            redaction = TurnCredentialRedaction(owner_key="display-owner", conversation_root=str(store.storage.root),
                                                thread_id=thread.thread_id, turn_key=request.conversation_turn_id)
        if redaction is not None:
            redaction.register_values((marker,))
        pending_ref = activity_sink.archive_tool_rows(iter(({"kind": "text", "text": f"{public} {marker}"},)))
        activity_sink.write_progress({
            "tool": "read_file", "phase": "completed", "round": 1, "call_index": 1,
            "ok": True, "output": public, "display_archive_ref": pending_ref,
        })
        activity_sink.write_thinking((public + "\n") * 800)
        background_execution._persist_background_native_turn(
            store, request, SimpleNamespace(internal_response=f"{public} {marker}"),
        )
        if scoped:
            assert read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"] == []
        return SimpleNamespace(response=public, archive_tool_calls=[{}])

    monkeypatch.setattr(background_execution, "run_background_turn_with_compact", run)
    try:
        result, snapshot = _invoke(agent, store, thread, request)
        commit = record_background_response(
            BackgroundDeliveryDependencies(agent, store, FakeDeliveryService(), lambda _: "completed"),
            request, DeliveryContext(channel="feishu", target="test-owner", thread_id=thread.thread_id),
            result.response, display_snapshot=snapshot, tool_call_count=1, deliver=True, delivery_reason=request.reason,
        )
        assert commit.persisted and commit.content == public
        projected = read_background_transcript_events(agent, thread_id=thread.thread_id, after=0)["events"]
        completed = next(event for event in projected if event["kind"] == "tool_completed")
        ref = completed["payload"]["display_archive_ref"]
        archived = read_display_archive_page(store, ref, 0)["rows"]
        assert archived[0]["text"].startswith(public)
        assert any(event["kind"] == "thinking_completed" for event in projected)
        if scoped:
            assert marker not in repr(archived)
            assert not _bytes_containing(tmp_path, marker.encode())
    finally:
        if redaction is not None:
            redaction.close()


def test_c128_publication():
    ports = ("activity", "archive", "history", "checkpoint", "final", "frozen_retry")
    participants = 128
    barrier = threading.Barrier(participants)

    def publish(index):
        gate = publication_gate_for(f"display-run-{index}")
        assert publication_gate_for(gate.run_id) is gate
        receipt = []
        for port in ports:
            gate.write(port, PublicationWrite((index, port), receipt.append))
        assert receipt == []
        barrier.wait(timeout=20)
        verified = index % 2 == 0
        gate.resolve("verified" if verified else "withheld")
        if not verified:
            for port in ports:
                gate.write(port, PublicationWrite((index, port, "withheld"), receipt.append, True))
        if verified:
            assert receipt[:len(ports)] == [(index, port) for port in ports]
        else:
            assert receipt == [(index, port, "withheld") for port in ports]
        return weakref.ref(gate)

    with ThreadPoolExecutor(max_workers=participants) as pool:
        references = list(pool.map(publish, range(participants)))
    assert all(reference() is None for reference in references)


def test_process_exit_during_pending_leaves_no_display_body(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", """
import os
import sys
from pathlib import Path

from agent_py_agent.tests.test_conveyor_m38_publication_gate import _case, _invoke
from agent_py_agent.agent.conversation import background_execution

agent, store, thread, request = _case(Path(sys.argv[1]))

def run(*_args, activity_sink, **_kwargs):
    activity_sink.write_thinking(('uncommitted-display-value' + '\\n') * 600)
    activity_sink.archive_tool_rows(iter(({'kind': 'text', 'text': 'uncommitted-display-value'},)))
    os._exit(71)

background_execution.run_background_turn_with_compact = run
_invoke(agent, store, thread, request)
""", str(tmp_path)],
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 71, result.stderr
    assert not _bytes_containing(tmp_path, b"uncommitted-display-value")


def test_pending_budget_overflow_discards_buffer(monkeypatch):
    from agent_py_agent.agent.conversation import background_transcript

    monkeypatch.setattr(background_transcript, "BACKGROUND_TRANSCRIPT_MAX_BYTES", 1024)
    receipt = []
    gate = publication_gate_for("display-budget-run")
    gate.write("history", PublicationWrite("small candidate", receipt.append))
    gate.write("archive", PublicationWrite("oversized candidate" * 1024, receipt.append))
    gate.resolve("verified")
    assert gate.decision == "withheld"
    assert receipt == []
    gate.write("checkpoint", PublicationWrite("withheld record", receipt.append, True))
    assert receipt == ["withheld record"]


def test_discarded_delivery_request_releases_pending_gate(tmp_path, monkeypatch):
    agent, store, thread, request = _case(tmp_path)

    def run(*_args, activity_sink, **_kwargs):
        activity_sink.write_thinking("pending display")
        return SimpleNamespace(response="pending final")

    monkeypatch.setattr(background_execution, "run_background_turn_with_compact", run)
    _invoke(agent, store, thread, request)
    reference = weakref.ref(request._background_publication_sink.publication_gate)
    assert reference() is not None and reference().decision == "pending"
    del request
    assert reference() is None


def test_transient_history_error_keeps_unapplied_entries_for_retry(tmp_path, monkeypatch):
    import errno

    agent, store, thread, request = _case(tmp_path)
    marker = "synthetic-display-value-30517"
    redaction = None

    def run(*_args, activity_sink, **_kwargs):
        nonlocal redaction
        redaction = TurnCredentialRedaction(owner_key="display-owner", conversation_root=str(store.storage.root),
                                            thread_id=thread.thread_id, turn_key=request.conversation_turn_id)
        redaction.register_values((marker,))
        background_execution._persist_background_native_turn(
            store, request, SimpleNamespace(internal_response=f"native receipt {marker}"),
        )
        activity_sink.write_thinking("public reasoning")
        return SimpleNamespace(response="approved final", archive_tool_calls=[{}])

    monkeypatch.setattr(background_execution, "run_background_turn_with_compact", run)
    original = store.messages.append_once
    failures = []

    def append_once(row, *, dedupe_key, **kwargs):
        if dedupe_key.startswith("background-native:") and not failures:
            failures.append(dedupe_key)
            raise OSError(errno.EIO, "simulated I/O error")
        return original(row, dedupe_key=dedupe_key, **kwargs)

    monkeypatch.setattr(store.messages, "append_once", append_once)
    deps = BackgroundDeliveryDependencies(agent, store, FakeDeliveryService(), lambda _: "completed")
    context = DeliveryContext(channel="feishu", target="test-owner", thread_id=thread.thread_id)
    try:
        result, snapshot = _invoke(agent, store, thread, request)
        gate = request._background_publication_sink.publication_gate
        with pytest.raises(OSError, match="simulated I/O error"):
            record_background_response(deps, request, context, result.response, display_snapshot=snapshot,
                                       tool_call_count=1, deliver=True, delivery_reason=request.reason)
        assert gate.decision == "verified" and not gate.settled
        assert not [row for row in store.messages.recent(thread.thread_id, limit=50)
                    if row.metadata.get("assistant_part_id") == "native"]
        commit = record_background_response(deps, request, context, result.response, display_snapshot=snapshot,
                                            tool_call_count=1, deliver=True, delivery_reason=request.reason)
        assert commit.persisted and commit.content == "approved final", list(gate._held)
        assert gate.settled, list(gate._held)
        rows = store.messages.recent(thread.thread_id, limit=50)
        native = [row for row in rows if row.metadata.get("assistant_part_id") == "native"]
        assert len(native) == 1 and len(failures) == 1
        # 续写沿首次核验选中的同一脱敏投影，原始值不落盘。
        assert marker not in repr(native[0].metadata)
        assert rows[-1].content == "approved final"
        assert rows.index(native[0]) < rows.index(rows[-1])
        assert not _bytes_containing(tmp_path, marker.encode())
    finally:
        if redaction is not None:
            redaction.close()


def test_gate_retry_resumes_failed_suffix_in_order_once():
    gate = publication_gate_for("display-retry-run")
    applied, attempts = [], []

    def flaky(value):
        attempts.append(value)
        if value in ("second", "late") and attempts.count(value) == 1:
            raise OSError("simulated I/O error")
        applied.append(value)

    for value in ("first", "second", "third"):
        gate.write("history", PublicationWrite(value, flaky))
    with pytest.raises(OSError):
        gate.resolve("verified")
    assert applied == ["first"] and not gate.settled
    # 终态已签发后仍有未应用条目：后到的写入排在其后，不越过失败条目。
    with pytest.raises(OSError):
        gate.write("history", PublicationWrite("late", flaky))
    assert applied == ["first", "second", "third"]
    gate.resolve("withheld")  # 决定不可改写，只续写剩余条目
    assert gate.decision == "verified" and gate.settled
    assert applied == ["first", "second", "third", "late"]
    assert attempts == ["first", "second", "second", "third", "late", "late"]
    gate.write("history", PublicationWrite("after", applied.append))
    assert applied[-1] == "after"


@pytest.fixture
def scheduler_case(tmp_path):
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
    from agent_py_agent.tests.test_scheduler_im_l_integration import im_case

    bind_admin_channel_identity(tmp_path / "home", "feishu", "ou_a")
    return im_case(tmp_path, delivery={"mode": "origin"})


def _scheduled_request(case, mode, *, credential):
    from agent_py_agent.agent.scheduler import delivery_policy
    from agent_py_agent.agent.scheduler.tool import ScheduleTool

    refs = [{"target_kind": "channel_binding", "binding_id": row["binding_id"]}
            for row in delivery_policy.available_delivery_targets(case.agent)]
    policy = {"mode": mode, "targets": refs, "include_origin": True}
    outcome = ScheduleTool(case.agent).execute({"action": "update_delivery", "job_id": case.job["job_id"],
                                                "expected_version": case.job["version"], "delivery_policy": policy})
    assert outcome.ok, outcome.output
    case.agent.scheduler_service.enqueue_ready_runs(now=case.job["next_run_at"])
    signal = case.store.wakes.pending()[0]
    wake = signal.to_dict()
    if credential:
        wake["metadata"] = {**(wake.get("metadata") or {}), "input_provider": "credential"}
    return signal, BackgroundRunRequest(thread_id=signal.thread_id, task_id=signal.root_task_id,
                                        reason=signal.reason, wake_signal=wake)


@pytest.mark.parametrize("mode", ["multi", "bound"])
@pytest.mark.parametrize("transport", ["sent", "rejected"])
def test_scheduler_targets_persist_only_the_safe_projection(scheduler_case, tmp_path, mode, transport):
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery

    case = scheduler_case
    marker = "synthetic-scheduler-value-58201"
    signal, request = _scheduled_request(case, mode, credential=True)
    channels = _RetryDelivery()
    channels.status = transport
    commit = record_background_response(
        BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "completed"), request,
        DeliveryContext("feishu", "ou_a", thread_id=case.thread.thread_id, task_id=request.task_id),
        f"scheduled report {marker}", tool_call_count=1, deliver=True, delivery_reason="scheduled_job_due",
    )
    receipts = case.agent.scheduler_repository.delivery_receipts(signal.root_task_id)
    seed = cached_owner_delivery(case.store.wakes.pending_one(signal.wake_signal_id))
    assert receipts and seed is not None
    assert marker not in commit.content and marker not in repr(receipts) and marker not in repr(seed)
    assert seed["message_metadata"]["publication_decision"] == "withheld"
    assert all(marker not in envelope.content for _, envelope, _ in channels.attempts)
    assert not _bytes_containing(tmp_path, marker.encode())
    if transport == "sent":
        assert commit.persisted and channels.attempts
        # 运输回执与目标状态照常保留。
        assert any(row.get("receipt_id") == "scheduled-receipt" for row in receipts.values())
        assert all(row["state"] == "committed" for row in receipts.values())
    else:
        assert not commit.persisted and any(row["state"] != "committed" for row in receipts.values())
        committed = {key for key, row in receipts.items() if row["state"] == "committed"}
        # 冷重投：新请求无执行接收器，只读冻结种子；已提交目标不再发送。
        from agent_py_agent.agent.conversation.background_delivery import frozen_owner_delivery

        channels.status, before = "sent", len(channels.attempts)
        retry = record_background_response(
            BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "completed"),
            BackgroundRunRequest(thread_id=request.thread_id, task_id=request.task_id, reason=request.reason,
                                 wake_signal=request.wake_signal),
            DeliveryContext("feishu", "ou_a", thread_id=case.thread.thread_id, task_id=request.task_id),
            seed["content"], tool_call_count=1, deliver=True, delivery_reason="scheduled_job_due",
            frozen_retry=frozen_owner_delivery(seed),
        )
        receipts = case.agent.scheduler_repository.delivery_receipts(signal.root_task_id)
        assert retry.persisted and marker not in retry.content and marker not in repr(receipts)
        assert len(channels.attempts) == before + 1
        assert all(receipts[key]["state"] == "committed" for key in committed)
        assert not _bytes_containing(tmp_path, marker.encode())


def test_scheduler_ordinary_business_response_is_unchanged(scheduler_case):
    from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery

    case = scheduler_case
    body = "ordinary scheduled report 40213"
    signal, request = _scheduled_request(case, "multi", credential=False)
    channels = _RetryDelivery()
    channels.status = "sent"
    commit = record_background_response(
        BackgroundDeliveryDependencies(case.agent, case.store, channels, lambda _: "completed"), request,
        DeliveryContext("feishu", "ou_a", thread_id=case.thread.thread_id, task_id=request.task_id),
        body, tool_call_count=1, deliver=True, delivery_reason="scheduled_job_due",
    )
    seed = cached_owner_delivery(case.store.wakes.pending_one(signal.wake_signal_id))
    assert commit.persisted and commit.content == body and seed["content"] == body
    metadata = seed["message_metadata"]
    assert "publication_decision" not in metadata and "credential_delivery_scoped" not in metadata
    assert "credential_redaction_verified" not in metadata
    assert [envelope.content for _, envelope, _ in channels.attempts] == [body]
