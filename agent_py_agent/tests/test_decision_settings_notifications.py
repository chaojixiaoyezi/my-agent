"""设置提交后精确通知及逆序/复合覆盖竞态；只使用原取消句柄，不创建资源池。"""
import uuid

import pytest

from agent_py_agent.agent.concurrency.interrupt import InterruptHandle
from agent_py_agent.agent.conversation import decision_model_call as calls
from agent_py_agent.agent.conversation import decision_policy as policy
from agent_py_agent.agent.conversation import decision_service as service
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.tests.test_decision_service import (
    decide,
    successful,
)
from agent_py_agent.tests.test_decision_service import prepared as _prepared
from agent_py_agent.tests.test_decision_settings import patch

prepared = _prepared


@pytest.fixture
def active(prepared):
    host, params, _ = prepared
    thread_id = params.task_attributes["conversation_thread_id"]
    view = settings(host, "read", {}, thread_id=thread_id)
    row = policy.ActiveDecision(policy.decision_owner_ref(host), thread_id, "recall", InterruptHandle(), host, view)
    token = uuid.uuid4().hex
    assert policy.register_active(token, row)
    try:
        yield row
    finally:
        policy.unregister_active(token, row)


def test_unrelated_point_change_keeps_current_request_and_advances_notification(active, prepared):
    host, _params, _ = prepared
    result = patch(host, {"points.model_selection.mode": "observe"})
    assert not active.handle.cancelled
    assert active.settings["revision"]["owner"] == result["revision"]["owner"]


def test_shadowed_owner_disable_then_thread_reset_cancels_immediately(prepared):
    host, params, _ = prepared
    thread_id = params.task_attributes["conversation_thread_id"]
    patch(host, {"enabled": True}, scope="thread", thread_id=thread_id)
    view = settings(host, "read", {}, thread_id=thread_id)
    row = policy.ActiveDecision(policy.decision_owner_ref(host), thread_id, "recall", InterruptHandle(), host, view)
    token = uuid.uuid4().hex
    assert policy.register_active(token, row)
    try:
        owner = patch(host, {"enabled": False})
        assert not row.handle.cancelled
        assert row.settings["revision"]["owner"] == owner["revision"]["owner"]
        current = settings(host, "read", {"scope": "thread"}, thread_id=thread_id)
        settings(host, "reset", {"scope": "thread", "expected_revision": current["revision"], "fields": ["enabled"]}, thread_id=thread_id)
        assert row.handle.cancelled and row.settings_cancelled
    finally:
        policy.unregister_active(token, row)


def test_reverse_notifications_cannot_revert_newer_snapshot(active, prepared, monkeypatch):
    host, _params, _ = prepared
    original = policy.notify_decision_settings_changed
    with monkeypatch.context() as capture:
        capture.setattr(policy, "notify_decision_settings_changed", lambda *_: None)
        older = patch(host, {"enabled": False})
        newer = patch(host, {"enabled": True})
    original(host, newer)
    original(host, older)
    assert not active.handle.cancelled
    assert active.settings["revision"]["owner"] == newer["revision"]["owner"]
    assert active.settings["overrides"]["owner"]["enabled"] is True


def test_thread_change_does_not_cancel_another_thread(active, prepared):
    host, _params, _ = prepared
    other = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice", "channel_conversation_id": "other"})
    patch(host, {"enabled": False}, scope="thread", thread_id=other.thread_id)
    assert not active.handle.cancelled


def test_notification_occurs_after_unlock_and_failure_does_not_undo_success(prepared, monkeypatch):
    host, params, _ = prepared
    seen = []
    def notified(context, result):
        readback = settings(context, "read", {}, thread_id=params.task_attributes["conversation_thread_id"], blocking=False)
        seen.append(readback["revision"])
        assert result["revision"]["owner"] == readback["revision"]["owner"]
        raise RuntimeError("notification failed")
    monkeypatch.setattr(policy, "notify_decision_settings_changed", notified)
    result = patch(host, {"enabled": False})
    assert result["ok"] and seen and result["effective"]["enabled"] is False


def test_close_between_registration_and_invoke_never_starts_call(prepared, monkeypatch):
    host, params, _ = prepared
    original = service.register_active
    def close_after_register(key, row):
        result = original(key, row)
        patch(host, {"enabled": False})
        return result
    monkeypatch.setattr(service, "register_active", close_after_register)
    monkeypatch.setattr(calls, "invoke_decision_model_call", lambda *_a, **_k: pytest.fail("closed request cannot start"))
    result = decide(host, params, service.begin_decision_stage(host, params, operation_id="batch"))
    assert result.status == "stale" and not result.may_apply


def test_close_during_final_validation_cannot_return_success(prepared, monkeypatch):
    host, params, _ = prepared
    original = service._stale
    checks = []
    def close_after_validation(*args):
        result = original(*args)
        checks.append(1)
        if len(checks) == 2:
            patch(host, {"enabled": False})
        return result
    monkeypatch.setattr(service, "_stale", close_after_validation)
    monkeypatch.setattr(calls, "invoke_decision_model_call", successful)
    result = decide(host, params, service.begin_decision_stage(host, params, operation_id="batch"))
    assert result.status == "stale" and result.reason == "settings_changed"
