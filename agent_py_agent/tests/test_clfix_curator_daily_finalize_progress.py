"""策展推进：待办积压不能把当天日终挤出时间窗，日终也守失败退避；输入读取失败熔断后只把坏的那类输入留在原游标，其余照常推进。"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch
from agent_py_agent.agent.memory_store.curator_models import (
    CURATOR_REPLAY_BREAKER_FAILURE_COUNT,
    CURATOR_REPLAY_BREAKER_OPEN,
    CURATOR_REPLAY_BREAKER_RETRY_SECONDS,
)
from agent_py_agent.tests.test_curator_observation_identity import _service
from agent_py_agent.tests.test_d2_curator_breaker_shrink_quarantine import _ShapeBackend, _thread


# 函数用途: 造一个按 UTC 计日、整天都在日终窗口内的策展服务；日终是否到期只看当天是否已收尾。
def _daily_case(tmp_path, count: int):
    store, thread, messages = _thread(tmp_path, count)
    backend = _ShapeBackend(thread.thread_id, lambda ids: False)
    service = _service(tmp_path, backend, store)
    service.timezone_name = "UTC"
    service.config = replace(service.config, daily_finalize_hour=0)
    return service, backend, store, thread, messages


# 函数用途: 写一份第二行不是 JSON 的审计账，让审计类输入在同一游标上确定性读取失败。
def _corrupt_audit(tmp_path) -> None:
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "audit.jsonl").write_text('{"event_id": "raw:1", "event": "tool"}\n{坏行\n', encoding="utf-8")


# 函数用途: 在同一起始游标上连续输入读取失败直到熔断打开，返回各次失败码与熔断时的 state。
def _trip_on_unreadable_audit(service, backend):
    codes = [service.run(reason="admin").failure_code for _ in range(CURATOR_REPLAY_BREAKER_FAILURE_COUNT)]
    assert all(code.startswith("CURATOR_INPUT_") for code in codes), codes
    assert backend.seen == [], "熔断前输入读取失败仍不调用模型"
    tripped = service.state_store.load()
    assert tripped.last_failure_code == CURATOR_REPLAY_BREAKER_OPEN
    return codes, tripped


def test_daily_finalize_takes_its_turn_while_other_reasons_stay_pending(tmp_path):
    service, _backend, _store, thread, messages = _daily_case(tmp_path, 2)
    now = datetime.now(timezone.utc)
    reasons = []
    for _ in range(2):
        # 前台不断登记新的会话收尾与任务完成待办，待办队列从不为空。
        service.request("session_close")
        service.request("task_complete")
        result = service.run_if_due(now=now)
        assert result.status == "succeeded", result
        reasons.append(result.reason)
    assert reasons == ["daily_finalize", "session_close"], "日终与待办轮流领拍，待办积压不能把日终挤出窗口"
    state = service.state_store.load()
    assert state.last_daily_finalize_date == now.date().isoformat()
    assert state.per_thread_cursors == {thread.thread_id: messages[-1].message_id}


def test_daily_finalize_waits_out_the_failure_backoff_like_pending_work(tmp_path):
    service, backend, _store, _thread_row, _messages = _daily_case(tmp_path, 1)
    backend.error = ConnectionResetError("连接被重置")
    assert service.run(reason="pre_compact").failure_code == "CURATOR_MODEL_FAILED"
    backend.error = None
    failed_at = datetime.fromisoformat(service.state_store.load().last_failure_at)
    attempts = len(backend.seen)

    soon = service.run_if_due(now=failed_at + timedelta(seconds=60))
    assert (soon.status, len(backend.seen)) == ("not_due", attempts), "失败退避期内日终也不重放同一批输入"

    later = failed_at + timedelta(minutes=11)
    finalized = service.run_if_due(now=later)
    assert (finalized.status, finalized.reason) == ("succeeded", "daily_finalize"), "退避中的那一拍不占日终轮次"
    assert service.state_store.load().last_daily_finalize_date == later.date().isoformat()


def test_breaker_open_load_error_keeps_the_broken_audit_at_its_cursor_and_finalizes_the_day(tmp_path):
    service, backend, _store, thread, messages = _daily_case(tmp_path, 1)
    _corrupt_audit(tmp_path)
    codes, tripped = _trip_on_unreadable_audit(service, backend)
    service.request("session_close")

    later = datetime.fromisoformat(tripped.last_failure_at) + timedelta(seconds=CURATOR_REPLAY_BREAKER_RETRY_SECONDS + 1)
    result = service.run_if_due(now=later)

    assert (result.status, result.reason, result.processed_messages) == ("succeeded", "daily_finalize", 1)
    state = service.state_store.load()
    assert state.last_daily_finalize_date == later.date().isoformat()
    assert state.per_thread_cursors == {thread.thread_id: messages[0].message_id}
    assert state.last_processed_audit_event_id == tripped.last_processed_audit_event_id, "坏审计账留在原游标，修好后重读，不跳过"
    assert f"curator_replay_breaker_isolated:audit:{codes[-1]}" in service.run_log.list()[-1].warnings


def test_isolation_holds_while_the_same_input_stays_unreadable_and_lifts_once_repaired(tmp_path):
    service, backend, store, thread, _messages = _daily_case(tmp_path, 1)
    _corrupt_audit(tmp_path)
    codes, tripped = _trip_on_unreadable_audit(service, backend)
    assert service.run(reason="admin").processed_messages == 1

    newer = store.messages.append({"thread_id": thread.thread_id, "role": "user", "channel": "internal",
                                   "content": "第 9 条：我改喝乌龙茶。", "now": 30.0,
                                   "metadata": {"session_id": "session-1", "request_id": "request-9"}})
    following = service.run(reason="admin")
    assert (following.status, following.processed_messages) == ("succeeded", 1), "坏输入没修好时继续隔离，不再重新失败三次、等熔断一小时"
    state = service.state_store.load()
    assert (state.last_failure_code, state.per_thread_cursors) == ("", {thread.thread_id: newer.message_id})
    assert state.last_processed_audit_event_id == tripped.last_processed_audit_event_id
    assert f"curator_replay_breaker_isolated:audit:{codes[-1]}" in service.run_log.list()[-1].warnings

    (tmp_path / "audit" / "audit.jsonl").write_text('{"event_id": "raw:1", "event": "tool"}\n', encoding="utf-8")
    repaired = service.run(reason="admin")
    assert repaired.status == "succeeded"
    assert not any(item.startswith("curator_replay_breaker_isolated") for item in service.run_log.list()[-1].warnings)


def test_isolation_needs_one_healthy_input_family():
    from agent_py_agent.agent.memory_store.curator import _isolate_breaker_load_errors

    context = SimpleNamespace(state_before=SimpleNamespace(last_failure_code=CURATOR_REPLAY_BREAKER_OPEN))
    audit_error = {"context": "memory_curator.audit_read", "error_code": "AUDIT_READ_FAILED"}
    message_error = {"context": "conversation.thread.list", "error_type": "JSONDecodeError"}
    both = CuratorInputBatch((), (), load_errors=(audit_error, message_error))
    assert _isolate_breaker_load_errors(None, context, both) is None, "两类输入都坏时没有可推进的部分，照原样失败"

    broken_messages = CuratorInputBatch((), (), load_errors=(message_error,), skipped_message_cursors={"t-1": "m-1"})
    isolated, warning = _isolate_breaker_load_errors(None, context, broken_messages)
    assert (isolated.load_errors, isolated.skipped_message_cursors) == ((), {}), "坏的消息类输入一条游标都不推进"
    assert warning == "curator_replay_breaker_isolated:messages:CURATOR_INPUT_READ_FAILED"

    closed = SimpleNamespace(state_before=SimpleNamespace(last_failure_code="CURATOR_INPUT_READ_FAILED"))
    no_history = SimpleNamespace(recent_finished=lambda count: [])
    assert _isolate_breaker_load_errors(no_history, closed, broken_messages) is None, "熔断前仍按原合同整批失败"
