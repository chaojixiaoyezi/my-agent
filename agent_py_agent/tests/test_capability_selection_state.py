"""一次能力选择只依附原 TaskLink；无模型、无后台线程，只验证原锁、身份和序列化。"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier

import pytest

from agent_py_agent.agent.conversation.capability_selection_state import (
    CAPABILITY_SELECTION_KEY,
    TaskCapabilitySelection,
)
from agent_py_agent.agent.conversation.models import ThreadTaskLink
from agent_py_agent.agent.conversation.store import ConversationStore


# LLM: 只生成结构化包身份，不代表安装、授权或正文读取；此层仅测试选择回执，不可把它当作 pins。
# 函数用途: 提供数量开放的合法引用集合，验证摘要记录不复制第二份引用权威。
def _refs(count=1):
    return tuple({
        "kind": "capability_package", "stable_id": f"capability:package-{index}",
        "name": f"package-{index}", "source": "capability_package",
        "content_sha256": hashlib.sha256(f"content-{index}".encode()).hexdigest(),
        "package_id": f"package-{index}", "activation_id": hashlib.sha256(f"active-{index}".encode()).hexdigest(),
    } for index in range(count))


# LLM: 使用真实 ConversationStore 新建线程及任务，资格只由独立 typed 参数初始化，不接受 request 自报键。
# 函数用途: 为 CAS、停止与重开测试建立同一原任务事实源。
def _store(tmp_path, *, pending=True):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "now": 1.0})
    link = store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-1", "goal": "整理材料", "now": 2.0},
                            capability_selection=TaskCapabilitySelection.pending() if pending else None)
    return store, link


# LLM: 宿主身份显式传入；callback 是本地执行权事实替身，测试不借任务文本生成权限。
# 函数用途: 按统一公开签名领取一次选择，允许用例覆盖执行身份或执行权复核。
def _claim(store, link, **overrides):
    values = dict(task_id=link.task_id, thread_id=link.thread_id, request_id="request-1", run_id="run-1",
                  attempt_id="attempt-1", candidate_digest="a" * 64, model_binding_digest="d" * 64,
                  execution_is_current=lambda latest, claim: latest.task_id == link.task_id and claim.attempt_id == "attempt-1")
    values.update(overrides)
    return store.tasks.claim_capability_selection(**values)


# LLM: 结算仅写 marker；此 helper 不调用包读取、pin 或上下文投影，防止把 selected 误作采用证明。
# 函数用途: 在原 TaskStore 中提交一次选择结果。
def _finish(store, link, claim, **overrides):
    values = dict(task_id=link.task_id, thread_id=link.thread_id, expected_claim=claim, outcome="empty",
                  execution_is_current=lambda latest, expected: latest.status == "active" and expected == claim)
    values.update(overrides)
    return store.tasks.finish_capability_selection(**values)


def test_absent_marker_preserves_original_task_json_bytes():
    link = ThreadTaskLink("thread", "task", "goal", skill_snapshot_refs=_refs())
    old_payload = asdict(link)
    old_payload.pop("capability_selection")
    old_payload.pop("capability_selection_corruption")
    assert CAPABILITY_SELECTION_KEY not in link.to_dict()
    assert (json.dumps(link.to_dict(), ensure_ascii=False, sort_keys=True, indent=2).encode()
            == json.dumps(old_payload, ensure_ascii=False, sort_keys=True, indent=2).encode())
    assert ThreadTaskLink.from_dict(old_payload).to_dict() == old_payload


def test_pending_only_initializes_new_link_through_typed_keyword(tmp_path):
    store, existing = _store(tmp_path, pending=False)
    forged = {CAPABILITY_SELECTION_KEY: TaskCapabilitySelection.pending().to_dict(),
              "capability_selection": TaskCapabilitySelection.pending().to_dict()}
    request = {"thread_id": existing.thread_id, "task_id": existing.task_id, "goal": "next", **forged}
    rebound = store.tasks.bind(request, capability_selection=TaskCapabilitySelection.pending())
    assert rebound.capability_selection is None
    fresh = store.tasks.bind({**request, "task_id": "fresh"})
    assert fresh.capability_selection is None
    with pytest.raises((TypeError, ValueError)):
        store.tasks.bind({**request, "task_id": "bad"}, capability_selection={"status": "pending"})
    assert not store.storage.task_path("bad").exists()


def test_claim_is_durable_once_across_concurrent_callers_and_restart(tmp_path):
    store, link = _store(tmp_path)
    barrier = Barrier(8)

    def claim(index):
        barrier.wait()
        return _claim(store, link, request_id=f"request-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    resumed = ConversationStore(store.storage.root)
    assert resumed.tasks.load(link.task_id).capability_selection == claimed[0]
    assert _claim(resumed, link) is None
    assert claimed[0].status == "claimed" and claimed[0].claim_id


@pytest.mark.parametrize("outcome, refs, warnings", [
    ("selected", _refs(40), ()), ("empty", (), ()), ("failed", (), ("CAPABILITY_SELECTION_MODEL_FAILED",)),
])
def test_finish_records_bounded_result_digest_without_copying_pins(tmp_path, outcome, refs, warnings):
    store, link = _store(tmp_path)
    claim = _claim(store, link)
    finished = _finish(store, link, claim, outcome=outcome, selected_refs=refs, warning_codes=warnings)
    assert finished.status == "finished" and finished.outcome == outcome
    assert finished.selected_count == len(refs)
    assert bool(finished.selection_digest) == (outcome != "failed")
    loaded = store.tasks.load(link.task_id)
    assert loaded.skill_snapshot_refs == ()
    assert "selected_refs" not in loaded.to_dict()[CAPABILITY_SELECTION_KEY]
    assert loaded.capability_selection == finished
    assert _finish(store, link, claim, outcome=outcome, selected_refs=refs, warning_codes=warnings) is None
    assert _claim(store, link) is None


@pytest.mark.parametrize("field, value", [("claim_id", "other"), ("attempt_id", "attempt-2"),
                                         ("candidate_digest", "b" * 64), ("model_binding_digest", "b" * 64)])
def test_changed_claim_cannot_finish(tmp_path, field, value):
    store, link = _store(tmp_path)
    claim = _claim(store, link)
    assert _finish(store, link, replace(claim, **{field: value})) is None
    assert store.tasks.load(link.task_id).capability_selection == claim


@pytest.mark.parametrize("digest", ["profile-menu-id", "D" * 64, "", None])
def test_claim_requires_exact_model_binding_digest_before_any_write(tmp_path, digest):
    store, link = _store(tmp_path)
    path = store.storage.task_path(link.task_id)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="CAPABILITY_SELECTION_MARKER_INVALID"):
        _claim(store, link, model_binding_digest=digest)
    assert path.read_bytes() == before


def test_authority_callback_blocks_claim_and_late_finish_without_writes(tmp_path):
    store, link = _store(tmp_path)
    path = store.storage.task_path(link.task_id)
    before = path.read_bytes()
    assert _claim(store, link, execution_is_current=lambda *_: False) is None
    assert path.read_bytes() == before
    claim = _claim(store, link)
    claimed_bytes = path.read_bytes()
    assert _finish(store, link, claim, execution_is_current=lambda *_: False) is None
    assert path.read_bytes() == claimed_bytes
    store.tasks.update_status({"task_id": link.task_id, "status": "interrupted"})
    assert _finish(store, link, claim) is None
    store.tasks.update_status({"task_id": link.task_id, "status": "active"})
    assert _claim(store, link) is None
    assert _finish(store, link, claim, execution_is_current=lambda *_: False) is None


def test_pin_goal_bind_and_stop_preserve_selection_marker(tmp_path):
    store, link = _store(tmp_path)
    claim = _claim(store, link)
    store.tasks.pin_skill_reference(task_id=link.task_id, thread_id=link.thread_id, reference=_refs()[0])
    store.tasks.update_goal({"task_id": link.task_id, "goal": "继续原目标"})
    store.tasks.bind({"thread_id": link.thread_id, "task_id": link.task_id, "goal": "rebind"},
                     capability_selection=TaskCapabilitySelection.pending())
    store.tasks.update_status({"task_id": link.task_id, "status": "interrupted"})
    latest = store.tasks.load(link.task_id)
    assert latest.capability_selection == claim and latest.skill_snapshot_refs == _refs()


@pytest.mark.parametrize("bad", [None, [], "pending", {"schema": CAPABILITY_SELECTION_KEY, "status": "PENDING"},
                                  {"schema": "old", "status": "pending"},
                                  {"schema": CAPABILITY_SELECTION_KEY, "status": "claimed"}])
def test_corrupt_marker_is_warning_only_and_original_value_survives_updates(tmp_path, bad):
    store, link = _store(tmp_path)
    path = store.storage.task_path(link.task_id)
    payload = json.loads(path.read_text())
    payload[CAPABILITY_SELECTION_KEY] = bad
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    loaded, error = store.tasks.load_report(link.task_id)
    assert error is None and loaded is not None
    assert loaded.capability_selection is None
    assert loaded.capability_selection_warning_codes == ("CAPABILITY_SELECTION_MARKER_INVALID",)
    assert _claim(store, link) is None and path.read_bytes() == before
    store.tasks.update_goal({"task_id": link.task_id, "goal": "正常任务仍可继续"})
    assert json.loads(path.read_text())[CAPABILITY_SELECTION_KEY] == bad


def test_rollback_ignores_then_drops_key_and_upgrade_never_backfills(tmp_path):
    store, link = _store(tmp_path)
    claim = _claim(store, link)
    path = store.storage.task_path(link.task_id)
    old_reader_payload = json.loads(path.read_text())
    old_reader_payload.pop(CAPABILITY_SELECTION_KEY)
    path.write_text(json.dumps(old_reader_payload), encoding="utf-8")
    reopened = ConversationStore(store.storage.root)
    rebound = reopened.tasks.bind({"thread_id": link.thread_id, "task_id": link.task_id},
                                  capability_selection=TaskCapabilitySelection.pending())
    assert rebound.capability_selection is None and rebound.capability_selection_warning_codes == ()
    assert _claim(reopened, link) is None and _finish(reopened, link, claim) is None
    assert CAPABILITY_SELECTION_KEY not in json.loads(path.read_text())


def test_wrong_task_or_thread_and_callback_failure_do_not_mutate_marker(tmp_path):
    store, link = _store(tmp_path)
    path = store.storage.task_path(link.task_id)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        _claim(store, link, thread_id="other-thread")

    def broken(*_):
        raise RuntimeError("authority unavailable")

    with pytest.raises(RuntimeError, match="authority unavailable"):
        _claim(store, link, execution_is_current=broken)
    assert path.read_bytes() == original
    payload = json.loads(original)
    payload["task_id"] = "other-task"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        _claim(store, link)


@pytest.mark.parametrize("outcome, refs, warnings", [
    ("selected", (), ()), ("empty", _refs(), ()), ("failed", _refs(), ("FAILED",)),
    ("selected", (*_refs(), *_refs()), ()), ("failed", (), ("body is not a code",)),
])
def test_invalid_results_never_consume_claim(tmp_path, outcome, refs, warnings):
    store, link = _store(tmp_path)
    claim = _claim(store, link)
    with pytest.raises(ValueError):
        _finish(store, link, claim, outcome=outcome, selected_refs=refs, warning_codes=warnings)
    assert store.tasks.load(link.task_id).capability_selection == claim


def test_concurrent_finish_has_one_winner_and_keeps_other_task_facts(tmp_path):
    store, link = _store(tmp_path)
    claim = _claim(store, link)
    barrier = Barrier(6)

    def finish(_):
        barrier.wait()
        return _finish(store, link, claim)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(finish, range(6)))
    assert len([result for result in results if result is not None]) == 1
    assert store.tasks.load(link.task_id).capability_selection.status == "finished"


def test_selection_ref_budget_is_bytes_not_closed_item_count(tmp_path):
    store, link = _store(tmp_path)
    claim = _claim(store, link)
    with pytest.raises(ValueError, match="CAPABILITY_SELECTION_RESULT_TOO_LARGE"):
        _finish(store, link, claim, outcome="selected", selected_refs=_refs(4000))
    assert store.tasks.load(link.task_id).capability_selection == claim
    result = _finish(store, link, claim, outcome="selected", selected_refs=_refs(64))
    assert result.selected_count == 64


def test_result_digest_is_set_stable_and_detached_from_callers():
    claim = TaskCapabilitySelection(status="claimed", claim_id="claim", request_id="request", run_id="run",
                                    attempt_id="attempt", candidate_digest="a" * 64, model_binding_digest="d" * 64)
    refs = list(_refs(3))
    first = claim.finished(outcome="selected", selected_refs=refs)
    assert first == claim.finished(outcome="selected", selected_refs=list(reversed(refs)))
    refs[0]["content_sha256"] = "b" * 64
    assert first.selection_digest != claim.finished(outcome="selected", selected_refs=refs).selection_digest
    payload = first.to_dict()
    payload["warning_codes"].append("MUTATED")
    assert first.warning_codes == ()


@pytest.mark.parametrize("warnings", [("A",) * 17, ("A" * 81,), ("A", "A")])
def test_warning_budget_rejects_without_truncation(warnings):
    with pytest.raises(ValueError):
        TaskCapabilitySelection(status="pending", warning_codes=warnings)


def test_pin_authority_is_checked_inside_both_original_locks_and_failure_keeps_bytes(tmp_path):
    from agent_py_agent.agent.gateway_parts.io import _path_lock

    store, link = _store(tmp_path)
    path = store.storage.task_path(link.task_id)
    before = path.read_bytes()
    calls = []

    def revoked():
        calls.append((_path_lock(path).locked(), _path_lock(store.storage.tasks_dir / ".task-1.transition").locked()))
        raise RuntimeError("attempt changed")

    with pytest.raises(RuntimeError, match="attempt changed"):
        store.tasks.pin_skill_reference(task_id=link.task_id, thread_id=link.thread_id, reference=_refs()[0],
                                       execution_authority_check=revoked)
    assert calls == [(True, True)]
    assert path.read_bytes() == before and store.tasks.load(link.task_id).skill_snapshot_refs == ()
    pinned = store.tasks.pin_skill_reference(task_id=link.task_id, thread_id=link.thread_id, reference=_refs()[0],
                                            execution_authority_check=lambda: None)
    assert pinned.skill_snapshot_refs == _refs()
    # 已pin的幂等读取也不能绕过新的执行权失效事实。
    with pytest.raises(RuntimeError, match="attempt changed"):
        store.tasks.pin_skill_reference(task_id=link.task_id, thread_id=link.thread_id, reference=_refs()[0],
                                       execution_authority_check=revoked)


def test_cas_checks_authority_under_original_locks_before_persisting(tmp_path):
    from agent_py_agent.agent.gateway_parts.io import _path_lock

    store, link = _store(tmp_path)
    path = store.storage.task_path(link.task_id)
    observations = []

    def current(latest, claim):
        assert _path_lock(path).locked()
        assert _path_lock(store.storage.tasks_dir / ".task-1.transition").locked()
        observations.append((latest.capability_selection.status, claim.status,
                             json.loads(path.read_text())[CAPABILITY_SELECTION_KEY]["status"]))
        return True

    claim = _claim(store, link, execution_is_current=current)
    assert observations == [("pending", "claimed", "pending")]
    assert _finish(store, link, claim, execution_is_current=current).status == "finished"
    assert observations[-1] == ("claimed", "claimed", "claimed")
