from __future__ import annotations

import errno
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.tools.handoff_board_tool import (
    ManageHandoffBoardTool,
)
from agent_py_agent.agent.collaboration import CollaborationCoordinator, CollaborationStore
from agent_py_agent.agent.collaboration.external_mailbox_adapter import (
    ExternalMailboxAdapter,
    create_external_mailbox_layout,
)
from agent_py_agent.agent.collaboration.handoff_board import HandoffBoardService
from agent_py_agent.agent.common.opaque_id import OpaqueIdError
from agent_py_agent.agent.conversation import ConversationStore


def _make_conversation_thread(tmp_path: Path):
    conversation = ConversationStore(tmp_path / "conversations")
    thread = conversation.threads.get_or_create(
        {
            "canonical_user_id": "user-nw2",
            "channel": "internal",
            "channel_conversation_id": "thread-nw2",
            "channel_user_id": "user-nw2",
            "now": 1.0,
        }
    )
    conversation.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-nw2",
            "goal": "NW2 W1 coordination regression",
            "now": 2.0,
        }
    )
    return conversation, thread


def test_nw01_coordinator_tick_queries_index_only_open_non_dirty_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation, thread = _make_conversation_thread(tmp_path)
    store = CollaborationStore(tmp_path / "collaboration")

    for idx in range(40):
        closed_case = store.open_case(
            {
                "thread_id": thread.thread_id,
                "task_id": "task-nw2",
                "title": f"Closed case {idx}",
                "summary": "Already closed",
                "priority": "normal",
                "created_by": "source-a",
                "now": float(idx + 1),
            }
        )
        store.update_case_status(
            closed_case.case_id,
            status="closed",
            now=float(idx + 2),
        )

    open_case = store.open_case(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-nw2",
            "title": "Active expired request case",
            "summary": "Needs timeout handling on tick",
            "priority": "normal",
            "created_by": "source-a",
            "now": 100.0,
        }
    )
    store.request_collaboration(
        {
            "case_id": open_case.case_id,
            "requester_agent_id": "source-a",
            "target_agent_ids": ("source-b",),
            "question": "Provide status",
            "deadline_at": 110.0,
            "now": 101.0,
        }
    )

    loaded_ids: list[str] = []
    orig_load_case = store.load_case

    def tracked_load_case(case_id: str):
        loaded_ids.append(case_id)
        return orig_load_case(case_id)

    def fail_list_cases(**_kwargs):
        raise AssertionError("tick() must not call store.list_cases() full scan")

    monkeypatch.setattr(store, "load_case", tracked_load_case)
    monkeypatch.setattr(store, "list_cases", fail_list_cases)

    coordinator = CollaborationCoordinator(store=store, conversation_store=conversation)
    decisions = coordinator.tick(now=120.0)

    assert len(decisions) == 1
    assert decisions[0].case_id == open_case.case_id
    assert set(loaded_ids) == {open_case.case_id}

    # Open a second case and simulate a dirty index entry + partial index meta flag.
    open_two = store.open_case(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-nw2",
            "title": "Healthy second open case",
            "summary": "Should still be processed",
            "priority": "normal",
            "created_by": "source-a",
            "now": 130.0,
        }
    )
    store.request_collaboration(
        {
            "case_id": open_two.case_id,
            "requester_agent_id": "source-a",
            "target_agent_ids": ("source-b",),
            "question": "Pending timeout",
            "deadline_at": 135.0,
            "now": 131.0,
        }
    )
    dirty_case = store.open_case(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-nw2",
            "title": "Dirty open case",
            "summary": "Recovery fails temporarily",
            "priority": "normal",
            "created_by": "source-a",
            "now": 132.0,
        }
    )
    store.request_collaboration(
        {
            "case_id": dirty_case.case_id,
            "requester_agent_id": "source-a",
            "target_agent_ids": ("source-b",),
            "question": "Pending dirty",
            "deadline_at": 135.0,
            "now": 133.0,
        }
    )
    store.history_index.mark_dirty(dirty_case.case_id, published=True)
    with store.history_index.connect() as db:
        db.execute("BEGIN")
        db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('cases_complete', '0')"
        )
    monkeypatch.setattr(
        store,
        "_prepare_history_read",
        lambda: [{"case_id": dirty_case.case_id, "error": "simulated_recovery_deferred"}],
    )

    loaded_ids.clear()
    with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.collaboration.coordinator"):
        decisions_partial = coordinator.tick(now=150.0)

    assert any("COLLABORATION_INDEX_DEFERRED" in rec.message for rec in caplog.records)
    assert dirty_case.case_id not in loaded_ids
    assert open_two.case_id in loaded_ids
    assert any(dec.case_id == open_two.case_id for dec in decisions_partial)

    # Corrupt index -> logs COLLABORATION_INDEX_UNAVAILABLE and returns [] with zero fallback scan.
    caplog.clear()
    monkeypatch.setattr(
        store,
        "_prepare_history_read",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("index unavailable")),
    )
    with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.collaboration.coordinator"):
        assert coordinator.tick(now=160.0) == []
    assert any("COLLABORATION_INDEX_UNAVAILABLE" in rec.message for rec in caplog.records)

    # Restore _prepare_history_read and verify per-case load error logs COLLABORATION_CASE_DEFERRED.
    case_three = store.open_case(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-nw2",
            "title": "Third open case",
            "summary": "Simulates per-case load error during tick",
            "priority": "normal",
            "created_by": "source-a",
            "now": 165.0,
        }
    )
    caplog.clear()
    monkeypatch.setattr(store, "_prepare_history_read", lambda: [])

    def flaky_load_case(case_id: str):
        if case_id == case_three.case_id:
            raise OSError(errno.EIO, "case json temporarily unreadable")
        return orig_load_case(case_id)

    monkeypatch.setattr(store, "load_case", flaky_load_case)
    with caplog.at_level(logging.WARNING, logger="agent_py_agent.agent.collaboration.coordinator"):
        coordinator.tick(now=170.0)
    assert any("COLLABORATION_CASE_DEFERRED" in rec.message for rec in caplog.records)


def test_nw02_active_cases_index_validates_opaque_ids_and_repairs_without_nested_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollaborationStore(tmp_path / "collaboration")
    service = HandoffBoardService(store=store, workspace_root=tmp_path / "workspace")

    c_active = store.open_case({"title": "Active", "summary": "s", "created_by": "coord"})
    c_closed = store.open_case({"title": "Closed", "summary": "s", "created_by": "coord"})
    c_corrupt_tail = store.open_case(
        {"title": "Corrupt tail", "summary": "s", "created_by": "coord"}
    )

    pub_item = service.publish_item(
        case_id=c_active.case_id,
        request_id="",
        responsible_actor="worker-1",
        frozen_input_version="v1",
        idempotency_key="pub-active",
    )
    closed_item = service.publish_item(
        case_id=c_closed.case_id,
        request_id="",
        responsible_actor="worker-2",
        frozen_input_version="v1",
        idempotency_key="pub-closed",
    )
    service.close_item(case_id=c_closed.case_id, item_id=closed_item.item_id, reason="done")

    tail_item = service.publish_item(
        case_id=c_corrupt_tail.case_id,
        request_id="",
        responsible_actor="worker-3",
        frozen_input_version="v1",
        idempotency_key="pub-tail",
    )
    service.close_item(case_id=c_corrupt_tail.case_id, item_id=tail_item.item_id, reason="done")
    with store._handoff_items_path(c_corrupt_tail.case_id).open("a", encoding="utf-8") as handle:
        handle.write("{broken json tail\n")

    idx_path = store.handoff_items_dir / ".active_cases.json"
    orig_handoff_lock = store.handoff_case_lock
    in_active_read = {"flag": False}

    def guarded_case_lock(case_id: str):
        if in_active_read["flag"]:
            raise AssertionError("handoff_case_lock must never nest inside _active_case_ids_locked")
        return orig_handoff_lock(case_id)

    monkeypatch.setattr(store, "handoff_case_lock", guarded_case_lock)

    corrupt_variants = [
        json.dumps({"active_cases": ["../outside", c_active.case_id]}),
        "{unclosed-json",
        json.dumps({"unexpected_key": [c_active.case_id]}),
        json.dumps(["bad/path"]),
        json.dumps([12345]),
    ]

    for raw_text in corrupt_variants:
        idx_path.write_text(raw_text, encoding="utf-8")
        in_active_read["flag"] = True
        try:
            items = store.list_all_handoff_items(state="published")
        finally:
            in_active_read["flag"] = False

        assert [item.item_id for item in items] == [pub_item.item_id]
        repaired = json.loads(idx_path.read_text(encoding="utf-8"))
        assert repaired == {
            "active_cases": sorted([c_active.case_id, c_corrupt_tail.case_id])
        }

    # Missing index file also reconstructs and writes .active_cases.json without handoff_case_lock.
    idx_path.unlink()
    in_active_read["flag"] = True
    try:
        items_missing = store.list_all_handoff_items(state="published")
    finally:
        in_active_read["flag"] = False
    assert [item.item_id for item in items_missing] == [pub_item.item_id]
    assert json.loads(idx_path.read_text(encoding="utf-8")) == {
        "active_cases": sorted([c_active.case_id, c_corrupt_tail.case_id])
    }

    with pytest.raises(OpaqueIdError):
        store._update_active_cases_index("../outside", is_active=True)

    monkeypatch.setattr(store, "handoff_case_lock", orig_handoff_lock)

    # Concurrent publish + close + list_all_handoff_items + index deletion must not deadlock.
    extra_cases = [
        store.open_case({"title": f"Concurrent {i}", "summary": "s", "created_by": "coord"})
        for i in range(4)
    ]

    def worker(idx: int) -> None:
        cid = extra_cases[idx].case_id
        published = service.publish_item(
            case_id=cid,
            request_id="",
            responsible_actor=f"w-{idx}",
            frozen_input_version="v1",
            idempotency_key=f"conc-{idx}",
        )
        if idx % 2 == 0:
            service.close_item(case_id=cid, item_id=published.item_id, reason="finished")
        store.list_all_handoff_items(state="published")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker, idx) for idx in range(4)]
        for fut in futures:
            fut.result(timeout=15)

    final_index = json.loads(idx_path.read_text(encoding="utf-8"))["active_cases"]
    assert c_active.case_id in final_index
    assert extra_cases[1].case_id in final_index
    assert extra_cases[3].case_id in final_index
    assert extra_cases[0].case_id not in final_index
    assert extra_cases[2].case_id not in final_index


def test_nw03_external_mailbox_holds_cursor_on_retryable_apply_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollaborationStore(tmp_path / "collaboration")
    service = HandoffBoardService(store=store, workspace_root=tmp_path / "workspace")
    case = store.open_case({"title": "Mailbox case", "summary": "s", "created_by": "coord"})

    item_ack = service.publish_item(
        case_id=case.case_id,
        request_id="",
        item_id="item_mailbox_ack",
        responsible_actor="ext_worker",
        actor_kind="external_assistant",
        frozen_input_version="v1",
        idempotency_key="mb-1",
    )
    item_sub = service.publish_item(
        case_id=case.case_id,
        request_id="",
        item_id="item_mailbox_sub",
        responsible_actor="ext_worker",
        actor_kind="external_assistant",
        frozen_input_version="v1",
        idempotency_key="mb-2",
    )
    service.record_ack(
        case_id=case.case_id,
        item_id=item_sub.item_id,
        recipient_ack={"actor_id": "ext_worker", "status": "claimed"},
        frozen_input_version="v1",
        ack_id="pre-ack-sub",
    )

    coord_root = tmp_path / "mailbox_root"
    coord_root.mkdir(parents=True, exist_ok=True)
    layout = create_external_mailbox_layout(coord_root, "ext_worker")
    adapter = ExternalMailboxAdapter(allowed_roots=[coord_root], max_file_bytes=262144)

    # First sync writes initial outbox items.
    adapter.sync_external_mailbox(service, layout)

    # Simulate pre-existing cursor for an earlier line, then append two inbox messages.
    prior_msg = json.dumps({"action": "noop", "case_id": "", "item_id": ""})
    ack_msg = json.dumps(
        {
            "action": "ack",
            "case_id": case.case_id,
            "item_id": item_ack.item_id,
            "ack_id": "ext-ack-1",
            "frozen_input_version": "v1",
            "recipient_ack": {"actor_id": "ext_worker", "status": "accepted"},
        }
    )
    submit_msg = json.dumps(
        {
            "action": "submit",
            "case_id": case.case_id,
            "item_id": item_sub.item_id,
            "submission_id": "ext-sub-1",
            "evidence_ids": ["ev-1"],
        }
    )
    inbox_path = layout.actor_inbox_path
    cursor_path = inbox_path.parent / ".coordinator_cursor.json"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text(f"{prior_msg}\n", encoding="utf-8")
    adapter.sync_external_mailbox(service, layout)
    cursor_before = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor_before["processed_lines"] == 1

    with inbox_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{ack_msg}\n{submit_msg}\n")

    # Inject OSError when applying submit (ack succeeds, submit hits OSError).
    orig_submit = service.submit_handoff

    def failing_submit(**_kwargs):
        raise OSError(errno.EIO, "simulated write fault during mailbox apply")

    monkeypatch.setattr(service, "submit_handoff", failing_submit)
    report_fail = adapter.sync_external_mailbox(service, layout)

    assert item_ack.item_id in report_fail["acked_item_ids"]
    assert report_fail["submitted_item_ids"] == []
    assert len(report_fail["errors"]) == 1
    err = report_fail["errors"][0]
    assert err["error_code"] == "MAILBOX_APPLY_IO_ERROR"
    assert err["effect_outcome"] == "unknown"
    assert err["retryable"] is True
    assert err["exception_type"] == "OSError"
    assert err["errno"] == errno.EIO
    assert str(layout.root_dir) in err["next_action"]
    assert layout.actor_id in err["next_action"]

    # Cursor MUST stay pinned at prior position so retry can re-process failed messages.
    cursor_after_fail = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor_after_fail == cursor_before

    # Retry succeeds idempotently on ack and applies submit cleanly, then advances cursor.
    monkeypatch.setattr(service, "submit_handoff", orig_submit)
    report_retry = adapter.sync_external_mailbox(service, layout)
    assert report_retry["errors"] == []
    assert item_sub.item_id in report_retry["submitted_item_ids"]

    cursor_after_retry = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor_after_retry["processed_lines"] == 3
    assert store.load_handoff_item(case.case_id, item_ack.item_id).state == "acked"
    assert store.load_handoff_item(case.case_id, item_sub.item_id).state == "submitted"


def test_nw2_p7_01_handoff_board_tool_effect_outcome_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollaborationStore(tmp_path / "collaboration")
    service = HandoffBoardService(store=store, workspace_root=tmp_path / "workspace")
    tool = ManageHandoffBoardTool(agent=None, service=service)

    open_res = tool.execute({"action": "open_case", "title": "Tool case", "summary": "s"})
    assert open_res.ok
    case_id = open_res.result_envelope["case"]["case_id"]

    pub_res = tool.execute(
        {
            "action": "publish_item",
            "case_id": case_id,
            "responsible_actor": "worker-a",
            "frozen_input_version": "v1",
            "idempotency_key": "tool-pub-1",
        }
    )
    assert pub_res.ok
    item_id = pub_res.result_envelope["item"]["item_id"]

    # 1. Pre-effect validation & missing resources keep effect_outcome == "not_started".
    for bad_params in (
        {"action": "unknown_action"},
        {"action": "publish", "case_id": ""},
        {"action": "publish", "case_id": "case_missing_999", "responsible_actor": "w", "frozen_input_version": "v1"},
        {"action": "publish", "case_id": "../bad", "responsible_actor": "w", "frozen_input_version": "v1"},
        {"action": "read", "limit": "not-an-int"},
        {"action": "decide_acceptance", "case_id": case_id, "item_id": item_id, "verdict": "accepted"},
    ):
        outcome = tool.execute(bad_params)
        assert not outcome.ok
        assert outcome.effect_outcome == "not_started"

    # 2. Read-only action raising OSError preserves effect_outcome == "not_started".
    monkeypatch.setattr(
        service,
        "list_items",
        lambda **_kw: (_ for _ in ()).throw(OSError(errno.EIO, "read fault")),
    )
    read_err = tool.execute({"action": "list_items", "case_id": case_id})
    assert not read_err.ok
    assert read_err.error_code == "INTERNAL_ERROR"
    assert read_err.effect_outcome == "not_started"
    assert read_err.result_envelope["details"]["exception_type"] == "OSError"

    # 3. Mutating actions raising OSError / RuntimeError report effect_outcome == "unknown"
    # with exception_type, optional errno, known case_id/item_id, and read-before-retry next_action.
    mutating_cases = [
        (
            store,
            "open_case",
            {"action": "open_case", "title": "Fail open", "summary": "s"},
            OSError(errno.EIO, "open_case write error"),
            False,
            False,
        ),
        (
            service,
            "publish_item",
            {
                "action": "publish",
                "case_id": case_id,
                "item_id": "item_pub_fail",
                "responsible_actor": "worker-b",
                "frozen_input_version": "v1",
            },
            OSError(errno.EIO, "publish write error"),
            True,
            True,
        ),
        (
            service,
            "record_ack",
            {"action": "claim", "case_id": case_id, "item_id": item_id},
            RuntimeError("unexpected ack failure"),
            True,
            True,
        ),
        (
            service,
            "submit_handoff",
            {"action": "submit_handoff", "case_id": case_id, "item_id": item_id},
            OSError(errno.ENOSPC, "submit disk full"),
            True,
            True,
        ),
        (
            service,
            "decide_acceptance",
            {
                "action": "decide_acceptance",
                "case_id": case_id,
                "item_id": item_id,
                "verdict": "accepted",
            },
            OSError(errno.EIO, "decide error"),
            True,
            True,
        ),
        (
            service,
            "close_item",
            {"action": "close_item", "case_id": case_id, "item_id": item_id, "reason": "done"},
            OSError(errno.EIO, "close error"),
            True,
            True,
        ),
    ]

    for target_obj, attr_name, req_params, raised_exc, expect_case, expect_item in mutating_cases:
        with monkeypatch.context() as mp:
            def _raiser(*_a, _exc=raised_exc, **_kw):
                raise _exc

            mp.setattr(target_obj, attr_name, _raiser)
            res = tool.execute(req_params)

        assert not res.ok
        assert res.error_code == "INTERNAL_ERROR"
        assert res.effect_outcome == "unknown"
        assert res.result_envelope["effect_outcome"] == "unknown"
        det = res.result_envelope["details"]
        assert det["exception_type"] == type(raised_exc).__name__
        if isinstance(raised_exc, OSError):
            assert det["errno"] == raised_exc.errno
        else:
            assert "errno" not in det
        if expect_case:
            assert det["case_id"] == case_id
        if expect_item:
            assert det["item_id"] == req_params["item_id"]
        assert "list_items" in det["next_action"] or "list_cases" in det["next_action"]
        assert res.result_envelope["next_action"] == det["next_action"]

    # sync_external_mailbox OSError inside adapter.sync_external_mailbox also yields unknown.
    mb_root = tmp_path / "tool_mb"
    mb_root.mkdir(parents=True, exist_ok=True)
    tool_with_ws = ManageHandoffBoardTool(
        agent=SimpleNamespace(root=tmp_path, workspace_root=tmp_path),
        service=HandoffBoardService(store=store, workspace_root=tmp_path),
    )
    with monkeypatch.context() as mp:
        mp.setattr(
            ExternalMailboxAdapter,
            "sync_external_mailbox",
            lambda *_a, **_kw: (_ for _ in ()).throw(OSError(errno.EIO, "mailbox sync fault")),
        )
        mb_err = tool_with_ws.execute(
            {
                "action": "sync_external_mailbox",
                "root_dir": str(mb_root),
                "actor_id": "ext_worker",
            }
        )
    assert not mb_err.ok
    assert mb_err.error_code == "INTERNAL_ERROR"
    assert mb_err.effect_outcome == "unknown"
    assert mb_err.result_envelope["details"]["errno"] == errno.EIO

    # Explicit exception.effect_outcome == "not_started" is respected even for mutating actions.
    class PreEffectError(RuntimeError):
        effect_outcome = "not_started"

    with monkeypatch.context() as mp:
        mp.setattr(
            service,
            "publish_item",
            lambda **_kw: (_ for _ in ()).throw(PreEffectError("refused before write")),
        )
        explicit_res = tool.execute(
            {
                "action": "publish",
                "case_id": case_id,
                "responsible_actor": "worker-b",
                "frozen_input_version": "v1",
            }
        )
    assert not explicit_res.ok
    assert explicit_res.error_code == "INTERNAL_ERROR"
    assert explicit_res.effect_outcome == "not_started"
