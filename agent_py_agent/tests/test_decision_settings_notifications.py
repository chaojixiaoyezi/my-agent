"""设置提交后精确通知及逆序/复合覆盖竞态；只使用原取消句柄，不创建资源池。"""
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

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
from agent_py_agent.tests.test_decision_settings import host_at, patch

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


def test_restore_notifies_once_and_cancels_newly_disabled_decision(prepared):
    host, params, _ = prepared
    thread_id = params.task_attributes["conversation_thread_id"]
    patch(host, {"enabled": True, "points.recall.mode": "observe"}, scope="thread", thread_id=thread_id)
    view = settings(host, "read", {}, thread_id=thread_id)
    row = policy.ActiveDecision(policy.decision_owner_ref(host), thread_id, "recall", InterruptHandle(), host, view)
    token = uuid.uuid4().hex
    assert policy.register_active(token, row)
    try:
        patch(host, {"enabled": False})
        assert not row.handle.cancelled
        current = settings(host, "read", {"scope": "thread"}, thread_id=thread_id)
        restored = settings(host, "restore", {"scope": "thread", "expected_revision": current["revision"],
            "set": {}, "unset": ["enabled"]}, thread_id=thread_id)
        assert restored["revision"]["thread"] == current["revision"]["thread"] + 1
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


# LLM: 复现“同一批在途请求按线程调度拿到不同结果码”的真实竞争。探针在**第一条收集完成**的那一刻，
#   非阻塞试取一次索引锁：整套写法此刻仍由调用方持锁（第二条必然已经收集完），逐条写法此刻已经放开锁
#   （第二条还没标记，它的发送线程能进来，_revoked 看不到撤销、_stale 只读到文件层 off，于是报 disabled）。
#   探针拿到锁就立刻放开，不和通知线程抢，也不重入持锁段（`_LOCK` 是普通 Lock）。
# 函数用途: 钉住“设置撤销对整批在途请求的结果码是确定的”。
def test_settings_change_marks_the_whole_batch_before_releasing_the_index_lock(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    thread_id = thread.thread_id
    patch(host, {"enabled": True, "points.recall.mode": "observe"}, scope="thread", thread_id=thread_id)
    view = settings(host, "read", {}, thread_id=thread_id)
    revision = view["revision"]["thread"]
    patch(host, {"points.recall.mode": "off"}, scope="thread", thread_id=thread_id)
    rows = [policy.ActiveDecision(policy.decision_owner_ref(host), thread_id, "recall", InterruptHandle(), host, view)
            for _ in range(2)]
    tokens = [uuid.uuid4().hex for _ in rows]
    for token, row in zip(tokens, rows):
        assert policy.register_active(token, row)
    real = policy._pending_advances
    collected = []
    window = {}

    def probe(targets, result):
        out = real(targets, result)
        collected.append(out)
        if len(collected) == 1:
            # 整套写法此刻仍由调用方持锁：别的线程进不来，第二条不可能先看到只有文件层变了。
            acquired = policy._LOCK.acquire(blocking=False)
            window["held"] = not acquired
            if acquired:
                revoked = service._revoked(rows[1], "observe")
                window["second"] = ("stale", revoked.reason) if revoked is not None else ("stale", "disabled")
                policy._LOCK.release()
        return out

    policy._pending_advances = probe
    try:
        policy.notify_decision_settings_changed(host, {
            "scope": "thread", "thread_id": thread_id, "revision": {"owner": 0, "thread": revision + 1},
            "overrides": {"owner": {}, "thread": {"points.recall.mode": "off"}}})
    finally:
        policy._pending_advances = real
        # 必须用登记时那把令牌注销；否则这些假在途请求会留在进程级索引里，污染后面按数量断言的用例。
        for token, row in zip(tokens, rows):
            policy.unregister_active(token, row)
    assert len(collected) == 1, "整套写法一次就该收集完，不该再重算"
    assert len(collected[0]) == 2, "整套在途请求必须一次收集完"
    assert window["held"] is True, "第一条收集完时锁必须还在调用方手里，别的线程不能开始发送"
    assert all(row.settings_cancelled for row in rows)
    assert all(row.handle.cancelled for row in rows)


# LLM: 路由比较会一路走到 decision_defaults（可能新建配置、读并解析能力配置 YAML），绝不能在全进程的
#   索引锁里跑——否则设置改动那一刻，所有在途决策线程的登记/注销/撤销检查都要等它。
#   用结构化事实判定：比较路由的整个过程中，当前线程都没有持有 _LOCK。
# 函数用途: 钉住“路由计算不在索引锁内执行”。
def test_routing_comparison_never_runs_while_holding_the_index_lock(tmp_path, monkeypatch):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    thread_id = thread.thread_id
    patch(host, {"enabled": True, "points.recall.mode": "observe"}, scope="thread", thread_id=thread_id)
    view = settings(host, "read", {}, thread_id=thread_id)
    rows = [policy.ActiveDecision(policy.decision_owner_ref(host), thread_id, "recall", InterruptHandle(), host, view)
            for _ in range(2)]
    tokens = [uuid.uuid4().hex for _ in rows]
    for token, row in zip(tokens, rows):
        assert policy.register_active(token, row)
    held = []
    real = policy.routing_signature

    def spy(context, values, point):
        held.append(policy._LOCK.locked())
        return real(context, values, point)

    monkeypatch.setattr(policy, "routing_signature", spy)
    try:
        policy.notify_decision_settings_changed(host, {
            "scope": "thread", "thread_id": thread_id, "revision": {"owner": 0, "thread": view["revision"]["thread"] + 1},
            "overrides": {"owner": {}, "thread": {"points.recall.mode": "off"}}})
    finally:
        for token, row in zip(tokens, rows):
            policy.unregister_active(token, row)
    assert held, "路由比较根本没跑"
    assert not any(held), f"路由计算在索引锁内执行了 {sum(held)} 次"


# LLM: 比路由期间插进来一条更新的通知时，整批要重算；用探针在第一次比路由时插入来固定这个交错，不靠 sleep。
#   插入的通知换的是**另一层**（owner 层）：它提交并推进全局通知代次，同时让本行的待合并快照变成新的一份——
#   这样“代次变了就重算”是唯一能拦住旧结论落盘的东西（若再进锁不查代次，本行就会用旧的收集结果写盘）。
# 函数用途: 钉住“比路由期间代次变了就整批重算，绝不拿旧结论写盘”。
def test_newer_notification_during_routing_recomputes_the_whole_batch(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    thread_id = thread.thread_id
    patch(host, {"enabled": True, "points.recall.mode": "observe"}, scope="thread", thread_id=thread_id)
    view = settings(host, "read", {}, thread_id=thread_id)
    rows = [policy.ActiveDecision(policy.decision_owner_ref(host), thread_id, "recall", InterruptHandle(), host, view)
            for _ in range(2)]
    tokens = [uuid.uuid4().hex for _ in rows]
    for token, row in zip(tokens, rows):
        assert policy.register_active(token, row)
    real = policy.routing_signature
    rounds = []

    def spy(context, values, point):
        rounds.append(values["revision"]["thread"])
        if len(rounds) == 2:
            # 第一轮收集已完成、正在比路由：插一条 owner 层通知，它会提交并推进通知代次。
            patch(host, {"points.recall.mode": "off"})
        return real(context, values, point)

    policy.routing_signature = spy
    try:
        policy.notify_decision_settings_changed(host, {
            "scope": "thread", "thread_id": thread_id, "revision": {"owner": 0, "thread": view["revision"]["thread"] + 1},
            "overrides": {"owner": {}, "thread": {"points.recall.mode": "off"}}})
    finally:
        policy.routing_signature = real
        for token, row in zip(tokens, rows):
            policy.unregister_active(token, row)
    assert len(rounds) >= 4, "代次变了必须整批重算（每轮两条各比一次路由）"
    assert all(row.settings_cancelled and row.handle.cancelled for row in rows)
    assert all(row.settings["revision"]["owner"] >= 1 for row in rows), "重算后的快照必须含 owner 层那次提交"


# LLM: 并发通知持续插入时不能无限重算；用尽上限就按“已改变”整批撤销（宁严勿松），绝不死循环。
#   探针在每次比路由时推进一次通知代次（等价于期间不断有更新的通知落盘），重算被上限挡住，最终整批被撤销。
# 函数用途: 钉住“重算超过上限时整批按变了处理”。
def test_exhausted_recompute_cancels_the_whole_batch(tmp_path):
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    thread_id = thread.thread_id
    patch(host, {"enabled": True, "points.recall.mode": "observe"}, scope="thread", thread_id=thread_id)
    view = settings(host, "read", {}, thread_id=thread_id)
    row = policy.ActiveDecision(policy.decision_owner_ref(host), thread_id, "recall", InterruptHandle(), host, view)
    token = uuid.uuid4().hex
    assert policy.register_active(token, row)
    real = policy.routing_signature
    rounds = []

    def spy(context, values, point):
        rounds.append(policy._action_generation)
        policy._action_generation += 1
        return real(context, values, point)

    policy.routing_signature = spy
    try:
        policy.notify_decision_settings_changed(host, {
            "scope": "thread", "thread_id": thread_id, "revision": {"owner": 0, "thread": view["revision"]["thread"] + 1},
            "overrides": {"owner": {}, "thread": {"points.recall.mode": "off"}}})
    finally:
        policy.routing_signature = real
        policy.unregister_active(token, row)
    assert len(rounds) <= 2 * policy._MAX_MARK_ATTEMPTS_COUNT, "重算必须被上限挡住，不能死循环"
    assert row.settings_cancelled, "重算用尽必须按‘变了’整批撤销"
    assert row.handle.cancelled
