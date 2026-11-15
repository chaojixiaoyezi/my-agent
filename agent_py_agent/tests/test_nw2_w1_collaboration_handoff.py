from __future__ import annotations

import builtins
import errno
import inspect
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
from agent_py_agent.agent.collaboration import (
    CollaborationCoordinator,
    CollaborationStore,
)
from agent_py_agent.agent.collaboration import (
    external_mailbox_adapter as mailbox_adapter_mod,
)
from agent_py_agent.agent.collaboration import (
    store as collab_store_mod,
)
from agent_py_agent.agent.collaboration import (
    store_common as collab_store_common_mod,
)
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


def test_nw2r1_p1_01_canonical_handoff_read_eio_holds_cursor_during_ack_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollaborationStore(tmp_path / "collaboration")
    service = HandoffBoardService(store=store, workspace_root=tmp_path / "workspace")
    case = store.open_case({"title": "Canonical EIO ACK", "summary": "s", "created_by": "coord"})
    item = service.publish_item(
        case_id=case.case_id,
        request_id="",
        item_id="item_canon_eio_ack",
        responsible_actor="ext_worker",
        actor_kind="external_assistant",
        frozen_input_version="v1",
        idempotency_key="canon-eio-ack",
    )

    coord_root = tmp_path / "mailbox_root"
    coord_root.mkdir(parents=True, exist_ok=True)
    layout = create_external_mailbox_layout(coord_root, "ext_worker")
    adapter = ExternalMailboxAdapter(allowed_roots=[coord_root], max_file_bytes=262144)
    first_sync = adapter.sync_external_mailbox(service, layout, now=10.0)
    assert first_sync["delivered_item_ids"] == [item.item_id]

    inbox_path = layout.actor_inbox_path
    cursor_path = inbox_path.parent / ".coordinator_cursor.json"
    prior_msg = json.dumps({"action": "noop", "case_id": "", "item_id": ""})
    inbox_path.write_text(f"{prior_msg}\n", encoding="utf-8")
    adapter.sync_external_mailbox(service, layout, now=11.0)
    cursor_before = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor_before["processed_lines"] == 1

    ack_msg = json.dumps(
        {
            "action": "ack",
            "case_id": case.case_id,
            "item_id": item.item_id,
            "ack_id": "ack-eio-1",
            "frozen_input_version": "v1",
            "recipient_ack": {"actor_id": "ext_worker", "status": "accepted"},
        }
    )
    with inbox_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{ack_msg}\n")

    target_handoff_path = store._handoff_items_path(case.case_id)
    orig_cached_read = collab_store_common_mod.read_text_lines_cached
    fault = {"active": True}

    def flaky_read_text_lines_cached(path: Path) -> tuple[str, ...]:
        if path == target_handoff_path and fault["active"]:
            raise OSError(errno.EIO, "simulated canonical handoff ledger read EIO")
        return orig_cached_read(path)

    monkeypatch.setattr(collab_store_common_mod, "read_text_lines_cached", flaky_read_text_lines_cached)

    fail_report = adapter.sync_external_mailbox(service, layout, now=12.0)
    assert fail_report["acked_item_ids"] == []
    assert len(fail_report["errors"]) == 1
    err = fail_report["errors"][0]
    assert err["error_code"] == "MAILBOX_APPLY_IO_ERROR"
    assert err["effect_outcome"] == "unknown"
    assert err["retryable"] is True
    assert json.loads(cursor_path.read_text(encoding="utf-8")) == cursor_before

    # Fault clears -> next clean sync applies ACK and advances cursor 1 -> 2.
    fault["active"] = False
    clean_report = adapter.sync_external_mailbox(service, layout, now=13.0)
    assert clean_report["errors"] == []
    assert clean_report["acked_item_ids"] == [item.item_id]
    assert json.loads(cursor_path.read_text(encoding="utf-8"))["processed_lines"] == 2
    assert store.load_handoff_item(case.case_id, item.item_id).state == "acked"


def test_nw2r1_p1_01_inbox_read_eio_after_stat_holds_cursor_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollaborationStore(tmp_path / "collaboration")
    service = HandoffBoardService(store=store, workspace_root=tmp_path / "workspace")
    case = store.open_case({"title": "Inbox EIO", "summary": "s", "created_by": "coord"})
    item = service.publish_item(
        case_id=case.case_id,
        request_id="",
        item_id="item_inbox_eio",
        responsible_actor="ext_worker",
        actor_kind="external_assistant",
        frozen_input_version="v1",
        idempotency_key="inbox-eio-1",
    )

    coord_root = tmp_path / "mailbox_root"
    coord_root.mkdir(parents=True, exist_ok=True)
    layout = create_external_mailbox_layout(coord_root, "ext_worker")
    adapter = ExternalMailboxAdapter(allowed_roots=[coord_root], max_file_bytes=262144)
    adapter.sync_external_mailbox(service, layout, now=20.0)

    inbox_path = layout.actor_inbox_path
    cursor_path = inbox_path.parent / ".coordinator_cursor.json"
    prior_msg = json.dumps({"action": "noop", "case_id": "", "item_id": ""})
    inbox_path.write_text(f"{prior_msg}\n", encoding="utf-8")
    adapter.sync_external_mailbox(service, layout, now=21.0)
    cursor_before = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor_before["processed_lines"] == 1

    ack_msg = json.dumps(
        {
            "action": "ack",
            "case_id": case.case_id,
            "item_id": item.item_id,
            "ack_id": "ack-inbox-eio",
            "frozen_input_version": "v1",
            "recipient_ack": {"actor_id": "ext_worker", "status": "accepted"},
        }
    )
    with inbox_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{ack_msg}\n")

    # Line-count open after stat succeeds (#1), then read_actor_messages open hits EIO (#2).
    orig_open = builtins.open
    open_counter = {"reads": 0, "active": True}

    def wrapped_open(
        file,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ):
        if open_counter["active"] and Path(file) == inbox_path and "r" in mode:
            open_counter["reads"] += 1
            if open_counter["reads"] == 2:
                raise OSError(errno.EIO, "simulated inbox read EIO after line-count stat")
        return orig_open(
            file,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(mailbox_adapter_mod, "open", wrapped_open, raising=False)

    fail_report = adapter.sync_external_mailbox(service, layout, now=22.0)
    assert fail_report["acked_item_ids"] == []
    assert any(
        entry.get("retryable") is True and "read_failed" in str(entry.get("reason") or "")
        for entry in fail_report["quarantined"]
    )
    assert json.loads(cursor_path.read_text(encoding="utf-8")) == cursor_before

    # Also verify stat_failed in read_actor_messages sets retryable=True and holds cursor.
    orig_stat = Path.stat
    stat_calls = {"active": True, "injected": False}

    def wrapped_stat(path_obj: Path, follow_symlinks: bool = True):
        frame = inspect.currentframe()
        caller_name = frame.f_back.f_code.co_name if frame and frame.f_back else ""
        if (
            stat_calls["active"]
            and not stat_calls["injected"]
            and path_obj == inbox_path
            and caller_name == "read_actor_messages"
        ):
            stat_calls["injected"] = True
            raise OSError(errno.EIO, "simulated inbox stat EIO in read_actor_messages")
        return orig_stat(path_obj, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as mp:
        open_counter["active"] = False
        mp.setattr(Path, "stat", wrapped_stat)
        stat_fail_report = adapter.sync_external_mailbox(service, layout, now=22.5)
    assert stat_calls["injected"] is True
    assert stat_fail_report["acked_item_ids"] == []
    assert any(
        entry.get("retryable") is True and "stat_failed" in str(entry.get("reason") or "")
        for entry in stat_fail_report["quarantined"]
    )
    assert json.loads(cursor_path.read_text(encoding="utf-8")) == cursor_before

    # Recovery: next clean sync delivers/applies unread message and advances cursor 1 -> 2.
    open_counter["active"] = False
    clean_report = adapter.sync_external_mailbox(service, layout, now=23.0)
    assert clean_report["errors"] == []
    assert clean_report["quarantined"] == []
    assert clean_report["acked_item_ids"] == [item.item_id]
    assert json.loads(cursor_path.read_text(encoding="utf-8"))["processed_lines"] == 2
    assert store.load_handoff_item(case.case_id, item.item_id).state == "acked"


def test_nw2r1_p1_01_publish_item_idempotent_retry_raises_on_read_eio_and_preserves_item_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollaborationStore(tmp_path / "collaboration")
    service = HandoffBoardService(store=store, workspace_root=tmp_path / "workspace")
    case = store.open_case({"title": "Publish EIO", "summary": "s", "created_by": "coord"})

    orig_item = service.publish_item(
        case_id=case.case_id,
        request_id="req-1",
        responsible_actor="worker-1",
        frozen_input_version="v1",
        exclusive_write_set=("a.py",),
        idempotency_key="stable-pub-key",
    )

    target_handoff_path = store._handoff_items_path(case.case_id)
    orig_cached_read = collab_store_common_mod.read_text_lines_cached
    fault = {"active": True}

    def flaky_read_text_lines_cached(path: Path) -> tuple[str, ...]:
        if path == target_handoff_path and fault["active"]:
            raise OSError(errno.EIO, "simulated canonical read EIO during idempotent publish")
        return orig_cached_read(path)

    monkeypatch.setattr(collab_store_common_mod, "read_text_lines_cached", flaky_read_text_lines_cached)

    with pytest.raises(OSError):
        service.publish_item(
            case_id=case.case_id,
            request_id="req-1",
            responsible_actor="worker-1",
            frozen_input_version="v1",
            exclusive_write_set=("a.py",),
            idempotency_key="stable-pub-key",
        )

    fault["active"] = False
    replayed = service.publish_item(
        case_id=case.case_id,
        request_id="req-1",
        responsible_actor="worker-1",
        frozen_input_version="v1",
        exclusive_write_set=("a.py",),
        idempotency_key="stable-pub-key",
    )
    assert replayed.item_id == orig_item.item_id
    all_items = store.case_handoff_items(case.case_id)
    assert [it.item_id for it in all_items] == [orig_item.item_id]


def test_nw2r1_p1_01_close_one_of_two_items_with_read_eio_does_not_evict_active_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollaborationStore(tmp_path / "collaboration")
    service = HandoffBoardService(store=store, workspace_root=tmp_path / "workspace")
    case = store.open_case({"title": "Two items close EIO", "summary": "s", "created_by": "coord"})

    first = service.publish_item(
        case_id=case.case_id,
        request_id="",
        responsible_actor="worker-1",
        frozen_input_version="v1",
        idempotency_key="two-1",
    )
    second = service.publish_item(
        case_id=case.case_id,
        request_id="",
        responsible_actor="worker-2",
        frozen_input_version="v1",
        idempotency_key="two-2",
    )

    idx_path = store.handoff_items_dir / ".active_cases.json"
    assert case.case_id in json.loads(idx_path.read_text(encoding="utf-8"))["active_cases"]

    target_handoff_path = store._handoff_items_path(case.case_id)
    orig_cached_read = collab_store_common_mod.read_text_lines_cached
    read_state = {"calls": 0}

    def flaky_read_text_lines_cached(path: Path) -> tuple[str, ...]:
        if path == target_handoff_path:
            read_state["calls"] += 1
            # Call #1: close_item -> load_handoff_item succeeds; Call #2: append_handoff_item remaining check hits EIO.
            if read_state["calls"] == 2:
                raise OSError(errno.EIO, "simulated EIO reading remaining items after close")
        return orig_cached_read(path)

    monkeypatch.setattr(collab_store_common_mod, "read_text_lines_cached", flaky_read_text_lines_cached)

    with pytest.raises(OSError):
        service.close_item(case_id=case.case_id, item_id=first.item_id, reason="first done")

    assert case.case_id in json.loads(idx_path.read_text(encoding="utf-8"))["active_cases"]
    assert store.load_handoff_item(case.case_id, first.item_id).state == "closed"

    published = store.list_all_handoff_items(state="published")
    assert [it.item_id for it in published] == [second.item_id]


def test_nw2r1_p1_02_active_index_write_ordering_and_idempotent_replay_heal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CollaborationStore(tmp_path / "collaboration")
    service = HandoffBoardService(store=store, workspace_root=tmp_path / "workspace")
    case_ordered = store.open_case({"title": "Ordered write", "summary": "s", "created_by": "coord"})

    idx_path = store.handoff_items_dir / ".active_cases.json"
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps({"active_cases": []}), encoding="utf-8")

    # Part A: Single EIO on .active_cases.json fails BEFORE canonical JSONL publish; retry succeeds cleanly.
    orig_write = collab_store_mod.write_json_file_atomic_unlocked
    fault = {"active": True}

    def flaky_write_index(path: Path, data: object, sort_keys: bool = True) -> None:
        if path == idx_path and fault["active"]:
            fault["active"] = False
            raise OSError(errno.EIO, "simulated active index write EIO")
        orig_write(path, data, sort_keys=sort_keys)

    monkeypatch.setattr(collab_store_mod, "write_json_file_atomic_unlocked", flaky_write_index)

    with pytest.raises(OSError):
        service.publish_item(
            case_id=case_ordered.case_id,
            request_id="",
            responsible_actor="ext_ordered",
            actor_kind="external_assistant",
            frozen_input_version="v1",
            idempotency_key="ordered-key",
        )

    assert not store._handoff_items_path(case_ordered.case_id).exists()
    assert json.loads(idx_path.read_text(encoding="utf-8")) == {"active_cases": []}

    published_ordered = service.publish_item(
        case_id=case_ordered.case_id,
        request_id="",
        responsible_actor="ext_ordered",
        actor_kind="external_assistant",
        frozen_input_version="v1",
        idempotency_key="ordered-key",
    )
    assert case_ordered.case_id in json.loads(idx_path.read_text(encoding="utf-8"))["active_cases"]
    assert [it.item_id for it in store.list_all_handoff_items(state="published")] == [published_ordered.item_id]

    # Part B: Pre-existing unindexed canonical non-closed item healed by idempotent publish retry.
    case_heal = store.open_case({"title": "Heal unindexed", "summary": "s", "created_by": "coord"})
    orig_heal = service.publish_item(
        case_id=case_heal.case_id,
        request_id="",
        responsible_actor="ext_heal",
        actor_kind="external_assistant",
        frozen_input_version="v1",
        idempotency_key="heal-key",
    )
    # Simulate an unindexed valid index file (missing case_heal.case_id).
    idx_path.write_text(json.dumps({"active_cases": [case_ordered.case_id]}), encoding="utf-8")
    assert store.list_all_handoff_items(state="published", responsible_actor="ext_heal") == []

    coord_root = tmp_path / "mailbox_heal"
    coord_root.mkdir(parents=True, exist_ok=True)
    layout = create_external_mailbox_layout(coord_root, "ext_heal")
    adapter = ExternalMailboxAdapter(allowed_roots=[coord_root], max_file_bytes=262144)
    assert adapter.sync_external_mailbox(service, layout, now=30.0)["delivered_item_ids"] == []

    replayed_heal = service.publish_item(
        case_id=case_heal.case_id,
        request_id="",
        responsible_actor="ext_heal",
        actor_kind="external_assistant",
        frozen_input_version="v1",
        idempotency_key="heal-key",
    )
    assert replayed_heal.item_id == orig_heal.item_id
    assert case_heal.case_id in json.loads(idx_path.read_text(encoding="utf-8"))["active_cases"]

    sync_heal = adapter.sync_external_mailbox(service, layout, now=31.0)
    assert sync_heal["delivered_item_ids"] == [orig_heal.item_id]
    assert layout.coordinator_outbox_path.exists()

    # Part C: Closed replay does NOT re-add closed case back into .active_cases.json.
    case_closed = store.open_case({"title": "Closed replay", "summary": "s", "created_by": "coord"})
    closed_pub = service.publish_item(
        case_id=case_closed.case_id,
        request_id="",
        responsible_actor="worker-closed",
        frozen_input_version="v1",
        idempotency_key="closed-replay-key",
    )
    service.close_item(case_id=case_closed.case_id, item_id=closed_pub.item_id, reason="done")
    assert case_closed.case_id not in json.loads(idx_path.read_text(encoding="utf-8"))["active_cases"]
    closed_replayed = service.publish_item(
        case_id=case_closed.case_id,
        request_id="",
        responsible_actor="worker-closed",
        frozen_input_version="v1",
        idempotency_key="closed-replay-key",
    )
    assert closed_replayed.item_id == closed_pub.item_id
    assert closed_replayed.state == "closed"
    assert case_closed.case_id not in json.loads(idx_path.read_text(encoding="utf-8"))["active_cases"]
