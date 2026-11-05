"""W8正文退役：真实任务/回报账，原输入去重、责任保护、CAS及逻辑容量对账。"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.session_task_report import result_report_key
from agent_py_agent.agent.conversation.session_task_retention import (
    maintain_session_task_payloads_lane,
)
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


def _fixture(tmp_path, *, days=31, handled=True, sender="sender"):
    home = home_paths(tmp_path / "isolated")
    home.owner_home_dir.mkdir(parents=True)
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    conversation = ConversationStore(home.owner_home_dir / "conversations")
    store = conversation.session_tasks
    sender = conversation.threads.create(owner_id="local/main", title="发送方").thread_id if sender else ""
    target = conversation.threads.create(owner_id="local/main", title="接收方").thread_id
    now = time.time()
    draft = SessionTaskDraft(sender, target, "原始任务正文" * 1500, body_dedupe_key="body-key", dedupe_key="input-key")
    task = store.create(draft, now=now - (days + 1) * 86400)
    store.advance(task.task_id, SessionTaskUpdate("accepted"), now=now - (days + 1) * 86400)
    task = store.advance(task.task_id, SessionTaskUpdate("done", summary="原始结果摘要" * 1500, result_refs=("artifact-ref",)),
                         now=now - days * 86400)
    if sender and handled:
        wake = conversation.wakes.raise_signal({"thread_id": sender, "reason": "session_message",
            "dedupe_key": result_report_key(task), "summary": "准确任务结果", "retain_handled": True})
        conversation.wakes.mark_handled(wake.wake_signal_id)
    return home, conversation, task, draft, now


def _request(home, now, *, cursor=None, commit=None, legal_hold=False, budget=None):
    return StorageLaneRequest("session_task_payloads", home, now, cursor or {}, budget or StorageLaneBudget(None, None, None),
                              legal_hold, commit is not None, None, commit)


def _drain(home, now, **kwargs):
    cursor, reports = {}, []
    for _ in range(32):
        receipt = validate_lane_receipt(maintain_session_task_payloads_lane(_request(home, now, cursor=cursor, **kwargs)))
        reports.append(receipt)
        if receipt["complete"]:
            return reports
        cursor = receipt["cursor"]
    pytest.fail("正文退役游标未完成")


def _metadata(store, task, metadata):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
    update_json_file_atomic(store._path(task.task_id), lambda data: {**data, "metadata": metadata}, require_existing=True)


def test_retirement_preserves_proofs_replay_and_only_removes_body_index(tmp_path):
    home, conversation, task, draft, now = _fixture(tmp_path)
    store = conversation.session_tasks
    dedupe = store._dedupe_path(task.dedupe_key)
    proof = dedupe.read_bytes()
    body = store._body_index_path(task.body_dedupe_key)
    before = store._path(task.task_id).stat().st_size + body.stat().st_size
    reports = _drain(home, now)
    saved = store.require(task.task_id)
    assert saved.status == "done" and saved.updated_at == task.updated_at
    assert saved.goal == saved.summary == "" and saved.result_refs == task.result_refs
    retired = saved.metadata["payload_retired"]
    assert retired == {"schema": "session_task_payload_retired.v1", "at": now,
        "goal_sha256": hashlib.sha256(task.goal.encode()).hexdigest(), "summary_sha256": hashlib.sha256(task.summary.encode()).hexdigest(),
        "goal_bytes": len(task.goal.encode()), "summary_bytes": len(task.summary.encode())}
    assert not body.exists() and dedupe.read_bytes() == proof
    assert sum(row["freed_bytes"] for row in reports) == before - store._path(task.task_id).stat().st_size
    assert store.create(draft).task_id == task.task_id
    with pytest.raises(ValueError, match="session task dedupe key reused with different input"):
        store.create(replace(draft, goal="另一个任务"))
    assert sum(row["deleted_items"] for row in _drain(home, now)) == 0


@pytest.mark.parametrize("metadata,reason", [
    ({"cancellation_control": {"outstanding_refs": ["attempt"]}}, "cancellation_pending"),
    ({"cancellation_control": {"model_worker_exit_unknown": True}}, "cancellation_pending"),
    ({"cancellation_control": {"resources_confirmed": False}}, "cancellation_pending"),
    ({"task_learning_enqueue": {"status": "pending"}}, "learning_pending"),
    ({"task_learning_enqueue": {"status": "ineligible", "reason": "canonical_completion_reader_unbound"}}, "learning_pending"),
    ({"task_learning_candidate": {"attempt_id": "original"}}, "learning_pending"),
    ({"task_learning_enqueue": "damaged"}, "learning_unknown"),
    ({"cancellation_control": {"outstanding_refs": ""}}, "cancellation_unknown"),
    ({"cancellation_control": {"resources_confirmed": None}}, "cancellation_unknown"),
    ({"task_learning_candidate": {}}, "learning_pending"),
])
def test_additional_outstanding_metadata_keeps_payload(tmp_path, metadata, reason):
    home, conversation, task, _, now = _fixture(tmp_path)
    _metadata(conversation.session_tasks, task, metadata)
    before = conversation.session_tasks._path(task.task_id).read_bytes()
    reports = _drain(home, now)
    assert any(row["protected"].get(reason) for row in reports)
    assert conversation.session_tasks._path(task.task_id).read_bytes() == before


@pytest.mark.parametrize("kind", ["report_pending", "learning_pending", "young", "pinned", "legal_hold", "pins_incomplete"])
def test_unsettled_young_pinned_or_held_task_is_kept(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.memory_store import retention_scan
    home, conversation, task, _, now = _fixture(tmp_path, days=29 if kind == "young" else 31,
                                               handled=kind != "report_pending")
    if kind == "learning_pending":
        wake = conversation.wakes.raise_signal({"thread_id": task.target_thread_id, "reason": "task_learning_completion", "summary": "未完成学习",
                                               "retain_handled": True})
        _metadata(conversation.session_tasks, task, {"task_learning_wake_id": wake.wake_signal_id})
    if kind in {"pinned", "pins_incomplete"}:
        pins = retention_scan._RetentionPins(home.owner_home_dir, ids={task.task_id} if kind == "pinned" else set(),
                                             complete=kind != "pins_incomplete")
        monkeypatch.setattr(retention_scan, "current_retention_pins", lambda *a, **kw: (pins, pins.complete))
    before = conversation.session_tasks._path(task.task_id).read_bytes()
    reports = _drain(home, now, legal_hold=kind == "legal_hold")
    assert conversation.session_tasks._path(task.task_id).read_bytes() == before
    assert sum(row["deleted_items"] for row in reports) == 0
    if kind in {"legal_hold", "pins_incomplete"}:
        assert reports[-1]["deferred_reason"] == kind


def test_real_handled_learning_wake_allows_retirement(tmp_path):
    home, conversation, task, _, now = _fixture(tmp_path)
    wake = conversation.wakes.raise_signal({"thread_id": task.target_thread_id, "reason": "task_learning_completion", "summary": "已完成学习",
                                           "retain_handled": True})
    conversation.wakes.mark_handled(wake.wake_signal_id)
    _metadata(conversation.session_tasks, task, {"task_learning_wake_id": wake.wake_signal_id,
                                               "task_learning_enqueue": {"status": "enqueued"}})
    _drain(home, now)
    assert conversation.session_tasks.require(task.task_id).goal == ""


def test_updated_at_cas_does_not_clear_newer_payload(tmp_path):
    home, conversation, task, _, now = _fixture(tmp_path)
    store = conversation.session_tasks
    def commit(operation):
        store.advance(task.task_id, SessionTaskUpdate("done", summary="晚到的新结果"), now=now)
        return operation()
    _drain(home, now, commit=commit)
    current = store.require(task.task_id)
    assert current.goal == task.goal and current.summary == "晚到的新结果"
    assert "payload_retired" not in current.metadata


def test_metadata_added_after_scan_is_rechecked_inside_cas(tmp_path):
    home, conversation, task, _, now = _fixture(tmp_path)
    store = conversation.session_tasks
    def commit(operation):
        _metadata(store, task, {"cancellation_control": {"outstanding_refs": ["late-resource"]}})
        return operation()
    _drain(home, now, commit=commit)
    current = store.require(task.task_id)
    assert current.goal == task.goal and current.metadata["cancellation_control"]["outstanding_refs"] == ["late-resource"]


def test_body_index_for_another_task_is_preserved(tmp_path):
    home, conversation, task, _, now = _fixture(tmp_path)
    store = conversation.session_tasks
    other = store.create(SessionTaskDraft("other", "target", "新任务", body_dedupe_key=task.body_dedupe_key))
    _drain(home, now)
    assert store.require(task.task_id).goal == ""
    assert store.load_by_body_dedupe_key(task.body_dedupe_key).task_id == other.task_id


def test_empty_owner_and_expired_budget_have_zero_commits(tmp_path):
    home = home_paths(tmp_path / "empty")
    home.owner_home_dir.mkdir(parents=True)
    commits = []
    receipt = maintain_session_task_payloads_lane(_request(home, time.time(), commit=lambda op: commits.append(op)))
    assert receipt["complete"] and commits == []
    home, conversation, task, _, now = _fixture(tmp_path / "populated")
    receipt = maintain_session_task_payloads_lane(_request(home, now, commit=lambda op: commits.append(op),
                                                          budget=StorageLaneBudget(0, None, None)))
    assert not receipt["complete"] and commits == [] and conversation.session_tasks.require(task.task_id).goal == task.goal


def test_owner_override_retires_two_day_payload(tmp_path):
    home, conversation, task, _, now = _fixture(tmp_path, days=2)
    home.owner_retention_json.write_text(json.dumps({**json.loads(home.owner_retention_json.read_text()), "session_task_payload_days": 1}))
    _drain(home, now)
    assert conversation.session_tasks.require(task.task_id).goal == ""


def test_exact_owner_quota_drop_matches_reported_freed_bytes(tmp_path):
    from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes

    home, _, _, _, now = _fixture(tmp_path)
    before = owner_logical_usage_bytes(home.owner_home_dir, exact=True)
    reports = _drain(home, now)
    after = owner_logical_usage_bytes(home.owner_home_dir, exact=True)
    assert before - after == sum(row["freed_bytes"] for row in reports) > 0


def test_event_ordered_late_bind_preserves_terminal_and_new_snapshot(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.conversation import session_tasks as module

    _, conversation, task, _, now = _fixture(tmp_path)
    store = conversation.session_tasks
    entered, release = threading.Event(), threading.Event()
    original = module.update_json_file_atomic

    def pause(path, updater, **kwargs):
        if threading.current_thread().name.startswith("retire-race"):
            entered.set()
            assert release.wait(5)
        return original(path, updater, **kwargs)

    monkeypatch.setattr(module, "update_json_file_atomic", pause)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="retire-race") as pool:
        pending = pool.submit(store.retire_payload, task.task_id, expected_updated_at=task.updated_at, now=now)
        try:
            assert entered.wait(5)
            store.bind_body(task.task_id, "late-guidance")
        finally:
            release.set()
        assert pending.result(timeout=5) is False
    current = store.require(task.task_id)
    assert current.status == "done" and current.goal == task.goal and current.body_guidance_id == "late-guidance"


def test_late_learning_wake_without_timestamp_change_is_kept(tmp_path):
    home, conversation, task, _, now = _fixture(tmp_path)
    store = conversation.session_tasks

    def commit(operation):
        wake = conversation.wakes.raise_signal({"thread_id": task.target_thread_id, "reason": "task_learning_completion",
                                               "summary": "晚到学习责任", "retain_handled": True})
        _metadata(store, task, {"task_learning_wake_id": wake.wake_signal_id})
        return operation()

    _drain(home, now, commit=commit)
    assert store.require(task.task_id).goal == task.goal


def test_retire_payload_does_not_create_locks_or_unlink_through_body_directory_symlink(tmp_path):
    _, conversation, task, _, now = _fixture(tmp_path)
    store = conversation.session_tasks
    body = store._body_index_path(task.body_dedupe_key)
    original_dir = body.parent
    original_dir.rename(original_dir.with_name("original-body"))
    outside = tmp_path / "outside-body"
    outside.mkdir()
    index = outside / body.name
    index.write_text(json.dumps({"task_id": task.task_id}))
    original_dir.symlink_to(outside, target_is_directory=True)
    before = index.read_bytes()
    assert store.retire_payload(task.task_id, expected_updated_at=task.updated_at, now=now)
    assert index.read_bytes() == before and list(outside.iterdir()) == [index]


@pytest.mark.parametrize("field,value", [
    ("schema_version", "session_task.v99"), ("updated_at", "unparseable"),
    ("updated_at", True), ("metadata", []),
])
def test_unknown_schema_time_or_metadata_never_grants_retirement(tmp_path, field, value):
    from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic

    home, conversation, task, _, now = _fixture(tmp_path)
    store = conversation.session_tasks
    path = store._path(task.task_id)
    update_json_file_atomic(path, lambda data: {**data, field: value}, require_existing=True)
    before = path.read_bytes()
    assert not store.retire_payload(task.task_id, expected_updated_at=task.updated_at, now=now)
    receipts = _drain(home, now)
    assert all(receipt["deleted_items"] == 0 for receipt in receipts)
    assert path.read_bytes() == before
