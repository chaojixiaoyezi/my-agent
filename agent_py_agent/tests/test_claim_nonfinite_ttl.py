"""损坏running TTL不能成为过期接管、恢复或审批资格；终态与旧有限值保持兼容。"""

import json
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore, store_claims
from agent_py_agent.agent.conversation.background_execution_view import (
    ExecutionViewError,
    validate_execution_claim,
)
from agent_py_agent.agent.runtime_errors import DataCorruptionError

_ABSENT = object()

def _case(tmp_path, value):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.create(owner_id="owner")
    claim = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "original",
            "reason": "test",
            "recover_same_task_only": True,
        }
    )
    path = store.storage.background_claim_path(thread.thread_id)
    raw = dict(claim)
    if value is _ABSENT:
        raw.pop("expires_at", None)
    else:
        raw["expires_at"] = value
    path.write_text(json.dumps(raw))
    return store, thread, claim, path


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "not-a-number", None, [], {}, ""]
)
def test_running_bad_ttl_is_read_error_and_cannot_reacquire_or_renew(tmp_path, value):
    store, thread, claim, path = _case(tmp_path, value)
    before = path.read_bytes()
    loaded, error = store.claims.load_report(thread.thread_id)
    assert loaded == {} and error
    assert "BACKGROUND_CLAIM_TTL_" in json.dumps(error)
    for recovery in (False, True):
        with pytest.raises(DataCorruptionError):
            store.claims.acquire(
                {
                    "thread_id": thread.thread_id,
                    "task_id": "original",
                    "reason": "recovery",
                    "recover_same_task_only": recovery,
                    "now": 1e15,
                }
            )
    with pytest.raises(DataCorruptionError):
        store.claims.renew({"thread_id": thread.thread_id, "claim_id": claim["claim_id"]})
    assert path.read_bytes() == before
    with pytest.raises(ExecutionViewError) as caught:
        validate_execution_claim(thread, {"status": "running", "expires_at": value})
    assert caught.value.code == "CLAIM_UNREADABLE"


@pytest.mark.parametrize("value", [0, "123.5", _ABSENT])
def test_legacy_finite_or_missing_ttl_still_uses_existing_recovery_rules(tmp_path, value):
    store, thread, claim, path = _case(tmp_path, value)
    loaded, error = store.claims.load_report(thread.thread_id)
    assert error is None and loaded["claim_id"] == claim["claim_id"]
    recovered = store.claims.acquire(
        {
            "thread_id": thread.thread_id,
            "task_id": "original",
            "reason": "recover",
            "recover_same_task_only": True,
            "now": 1000,
        }
    )
    assert recovered is not None and recovered["claim_id"] != claim["claim_id"]


def test_terminal_record_does_not_use_stale_ttl_as_execution_authority(tmp_path):
    store, thread, claim, path = _case(tmp_path, float("nan"))
    for status in ("finished", "failed", "cancelled"):
        raw = dict(claim, status=status, expires_at=float("nan"))
        path.write_text(json.dumps(raw))
        loaded, error = store.claims.load_report(thread.thread_id)
        assert error is None and loaded["status"] == status


def test_bad_running_ttl_does_not_reactivate_under_acquire_renew_interleaving(
    tmp_path, monkeypatch
):
    store, thread, claim, path = _case(tmp_path, float("nan"))
    before = path.read_bytes()
    entered, release, renew_started = threading.Event(), threading.Event(), threading.Event()
    original = store_claims.running_claim_expiry
    failures = []

    def hold_inside_original_claim_lock(data):
        if threading.current_thread().name == "blocked-acquire":
            entered.set()
            assert release.wait(5)
        return original(data)

    monkeypatch.setattr(store_claims, "running_claim_expiry", hold_inside_original_claim_lock)

    def acquire():
        try:
            store.claims.acquire(
                {
                    "thread_id": thread.thread_id,
                    "task_id": "original",
                    "reason": "retry",
                    "recover_same_task_only": True,
                }
            )
        except DataCorruptionError:
            failures.append("acquire")

    def renew():
        renew_started.set()
        try:
            store.claims.renew({"thread_id": thread.thread_id, "claim_id": claim["claim_id"]})
        except DataCorruptionError:
            failures.append("renew")

    first = threading.Thread(target=acquire, name="blocked-acquire")
    second = threading.Thread(target=renew)
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert renew_started.wait(5)
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert sorted(failures) == ["acquire", "renew"] and path.read_bytes() == before
    # 原执行者仍能按准确claim提交终态，不让坏TTL复活旧任务或丢取消证明。
    closed = store.claims.finish(
        {"thread_id": thread.thread_id, "claim_id": claim["claim_id"], "status": "cancelled"}
    )
    assert closed["status"] == "cancelled"
    assert store.claims.load_report(thread.thread_id)[0]["status"] == "cancelled"
