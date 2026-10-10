"""连续插话保留发送顺序，未知 POST 与迟到回执不重复或改投任务。"""
from dataclasses import replace

from agent_py_agent.cli.chat_parts.tui_input_delivery import TuiActiveInputOutboxEntry
from agent_py_agent.tests.test_tui_input import _auth_rejecting_reconciler


def test_outbox_restores_send_order_not_opaque_id_order(tmp_path):
    path = tmp_path / "outbox.json"
    reconciler = _auth_rejecting_reconciler(path, [], [])
    for key in ("z-first", "a-second"):
        reconciler.enqueue(TuiActiveInputOutboxEntry(key, "turn-a", key, key))
    restored = _auth_rejecting_reconciler(path, [], [])
    assert list(restored._read_entries()) == ["z-first", "a-second"]


def test_unknown_first_post_holds_later_post_but_not_status_poll(tmp_path):
    from agent_py_agent.cli.chat_parts.tui_input_delivery import _dispatchable_entries

    first = TuiActiveInputOutboxEntry("first", "turn-a", "first", "first", next_attempt_at=1000)
    later = replace(first, message_id="later", next_attempt_at=0)
    status = replace(first, message_id="status", request_id="gateway-known", next_attempt_at=0)
    assert _dispatchable_entries({"first": first, "later": later, "status": status}) == [first, status]


def test_late_duplicate_queued_receipt_attaches_once_without_changing_execution(tmp_path):
    import threading
    from queue import Queue
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.execution_target import GatewayExecutionTarget
    from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions
    from agent_py_agent.cli.chat_parts.tui_actions import _tui_attach_gateway_job
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("late-receipts")
    target = GatewayExecutionTarget("thread-a", "task", "task-a", "claim-a").to_dict()
    runtime.publish_execution_target(target)
    params = SimpleNamespace(tui_runtime=runtime, jobs=Queue(), state_lock=threading.Lock(),
                             pending_jobs_ref_for_enqueue=[0])
    for request_id in ("request-d", "request-c", "request-d", "request-c"):
        _tui_attach_gateway_job(params, request_id, display_text=request_id, gateway_request_id=request_id,
                                client_message_id=request_id, execution_options=GatewayAskExecutionOptions())
    assert params.jobs.qsize() == 2 and params.pending_jobs_ref_for_enqueue == [2]
    assert runtime.execution_target() == target


def test_failed_attachment_can_retry_same_receipt(tmp_path, monkeypatch):
    import threading
    from queue import Queue
    from types import SimpleNamespace

    import pytest

    from agent_py_agent.agent.gateway_parts.request_client import GatewayAskExecutionOptions
    from agent_py_agent.cli.chat_parts.tui_actions import _tui_attach_gateway_job
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("failed-attachment")
    params = SimpleNamespace(tui_runtime=runtime, jobs=Queue(), state_lock=threading.Lock(),
                             pending_jobs_ref_for_enqueue=[0])
    original = runtime.enqueue_prompt
    monkeypatch.setattr(runtime, "enqueue_prompt", lambda *_a, **_k: (_ for _ in ()).throw(OSError("display failed")))
    kwargs = dict(display_text="合成输入", gateway_request_id="request-c", client_message_id="input-c",
                  execution_options=GatewayAskExecutionOptions())
    with pytest.raises(OSError, match="display failed"):
        _tui_attach_gateway_job(params, "合成输入", **kwargs)
    assert params.pending_jobs_ref_for_enqueue == [0] and params.jobs.empty()
    monkeypatch.setattr(runtime, "enqueue_prompt", original)
    _tui_attach_gateway_job(params, "合成输入", **kwargs)
    _tui_attach_gateway_job(params, "合成输入", **kwargs)
    assert params.jobs.qsize() == 1 and params.pending_jobs_ref_for_enqueue == [1]


def test_outbox_retry_attaches_after_pending_row_was_already_removed(tmp_path, monkeypatch):
    import threading
    from queue import Queue
    from types import SimpleNamespace

    from agent_py_agent.cli.chat_client_context import (
        ActiveTurnInputDelivery,
        ActiveTurnInputResult,
    )
    from agent_py_agent.cli.chat_parts.tui_actions import _ensure_active_input_reconciler
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime

    runtime = TuiRuntime("receipt-retry")
    stopped = threading.Event()
    stopped.set()
    calls = []

    def response(*_args, **_kwargs):
        calls.append(1)
        return ActiveTurnInputResult(ActiveTurnInputDelivery.QUEUED, request_id="gateway-c")

    params = SimpleNamespace(
        tui_runtime=runtime, jobs=Queue(), state_lock=threading.Lock(), pending_jobs_ref_for_enqueue=[0],
        paths=SimpleNamespace(root=tmp_path), current_session_id="receipt-retry", stop_event=stopped,
        agent=SimpleNamespace(request_active_turn_input=response, request_active_turn_input_status=response),
    )
    reconciler = _ensure_active_input_reconciler(params)
    entry = TuiActiveInputOutboxEntry("message-c", "old-turn", "补充", "补充")
    runtime.enqueue_active_turn_input(entry.message_id, entry.display_text)
    reconciler.enqueue(entry)
    entry = reconciler._read_entries()[entry.message_id]
    original = runtime.enqueue_prompt
    monkeypatch.setattr(runtime, "enqueue_prompt", lambda *_a, **_k: (_ for _ in ()).throw(OSError("display failed")))
    reconciler._reconcile_one(entry)
    pending = reconciler._read_entries()[entry.message_id]
    assert pending.request_id == "gateway-c" and params.jobs.empty()
    monkeypatch.setattr(runtime, "enqueue_prompt", original)
    reconciler._reconcile_one(pending)
    assert reconciler._read_entries() == {}
    assert params.jobs.qsize() == 1 and params.jobs.get_nowait().gateway_request_id == "gateway-c"
    assert params.pending_jobs_ref_for_enqueue == [1] and len(calls) == 2
