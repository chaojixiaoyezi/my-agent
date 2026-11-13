"""同请求重试不能让中断的 final 吞掉后续完成结果。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import request_history as history


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path)
    thread = store.threads.create()
    monkeypatch.setattr(history, "index_conversation_message", lambda *args: None)
    return SimpleNamespace(conversation_store=store), SimpleNamespace(thread_id=thread.thread_id)


def append(agent, conversation, attempt, reason, content):
    return history.append_gateway_conversation_message(
        agent, {"execution_attempt_id": attempt}, conversation,
        request_id="request", role="assistant", content=content,
        assistant_turn=history.GatewayAssistantTurn(end_reason=reason),
    )


@pytest.mark.parametrize("attempts", [("", ""), ("one", "one"), ("one", "two")])
@pytest.mark.parametrize("reason", ["error", "interrupted", "aborted", "max-tokens", "blocked"])
def test_completed_retry_remains_visible_and_idempotent(runtime, attempts, reason):
    agent, conversation = runtime
    assert append(agent, conversation, attempts[0], reason, "失败" * 264)
    assert append(agent, conversation, attempts[1], "completed", "成功" * 1030)
    assert append(agent, conversation, attempts[1], "completed", "成功" * 1030)
    rows = agent.conversation_store.messages.recent(conversation.thread_id, limit=10)
    assert [(len(row.content), row.metadata["turn_end_reason"]) for row in rows] == [
        (528, reason), (2060, "completed")]


def test_legacy_final_without_reason_stays_idempotent(runtime):
    agent, conversation = runtime
    assert append(agent, conversation, "", "", "原回复")
    assert append(agent, conversation, "", "completed", "重复回复")
    assert len(agent.conversation_store.messages.recent(conversation.thread_id, limit=10)) == 1


def test_repair_keeps_both_attempts_and_completed_retry(runtime):
    agent, conversation = runtime
    for attempt, reason, content in [("one", "error", "中断"), ("one", "completed", "完成")]:
        history._queue_gateway_conversation_repair(
            agent, {"execution_attempt_id": attempt}, conversation,
            request_id="request", role="assistant", content=content,
            assistant_turn=history.GatewayAssistantTurn(end_reason=reason),
        )
    root = agent.conversation_store.storage.root
    assert len(list((root / "message_repairs").glob("*.json"))) == 2
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert not errors
    rows = agent.conversation_store.messages.recent(conversation.thread_id, limit=10)
    assert [row.content for row in rows] == ["中断", "完成"]
    assert len({row.content for row in rows}) == len(rows)


@pytest.mark.parametrize("late_reason", ["error", "interrupted", "completed", ""])
def test_completed_final_survives_late_attempt_repair(runtime, late_reason):
    agent, conversation = runtime
    assert append(agent, conversation, "new", "completed", "成功终稿")
    history._queue_gateway_conversation_repair(
        agent, {"execution_attempt_id": "old"}, conversation,
        request_id="request", role="assistant", content="迟到的旧稿",
        assistant_turn=history.GatewayAssistantTurn(end_reason=late_reason),
    )
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert not errors
    assert append(agent, conversation, "old", late_reason, "迟到的旧稿")
    rows = agent.conversation_store.messages.recent(conversation.thread_id, limit=10)
    assert [row.content for row in rows] == ["成功终稿"]


@pytest.mark.parametrize("late", [False, True])
def test_repair_commentary_precedes_final_and_old_attempt_cannot_follow(runtime, late):
    agent, conversation = runtime
    if late:
        assert append(agent, conversation, "new", "completed", "最终答复")
    history._queue_gateway_conversation_repair(
        agent, {"execution_attempt_id": "old" if late else "new"}, conversation,
        request_id="request", role="assistant", content="先看配置",
        assistant_part_id="commentary:1",
    )
    if late:
        assert history.append_gateway_conversation_message(
            agent, {"execution_attempt_id": "old"}, conversation,
            request_id="request", role="assistant", content="迟到的说明",
            assistant_part_id="commentary:2",
        )
    if not late:
        history._queue_gateway_conversation_repair(
            agent, {"execution_attempt_id": "new"}, conversation,
            request_id="request", role="assistant", content="最终答复",
            assistant_turn=history.GatewayAssistantTurn(end_reason="completed"),
        )
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert not errors
    rows = agent.conversation_store.messages.recent(conversation.thread_id, limit=10)
    assert [row.content for row in rows] == (["最终答复"] if late else ["先看配置", "最终答复"])


def test_queued_completed_attempts_replay_in_host_chronological_order(runtime, monkeypatch):
    agent, conversation = runtime
    clock = iter([10, 20])
    monkeypatch.setattr(history.time, "time_ns", lambda: next(clock))
    # 两个身份的文件名哈希顺序与宿主排队时钟相反。
    for attempt, content in [("two", "先完成"), ("one", "后完成")]:
        history._queue_gateway_conversation_repair(
            agent, {"execution_attempt_id": attempt}, conversation,
            request_id="request", role="assistant", content=content,
            assistant_turn=history.GatewayAssistantTurn(end_reason="completed"),
        )
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert not errors
    assert [row.content for row in agent.conversation_store.messages.recent(conversation.thread_id)] == ["先完成"]


def test_legacy_final_filename_is_still_replayed(runtime):
    import json

    agent, conversation = runtime
    root = agent.conversation_store.storage.root / "message_repairs"
    root.mkdir()
    (root / "request-assistant.json").write_text(json.dumps({
        "thread_id": conversation.thread_id, "role": "assistant", "content": "旧版本答复",
        "metadata": {"gateway_request_id": "request"},
    }))
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert not errors
    assert [row.content for row in agent.conversation_store.messages.recent(conversation.thread_id)] == ["旧版本答复"]


def test_completed_outside_tail_and_missing_index_defers_without_overwrite(runtime):
    agent, conversation = runtime
    store = agent.conversation_store
    store.messages.append({"thread_id": conversation.thread_id, "role": "assistant", "content": "权威终稿",
                           "metadata": {"gateway_request_id": "request", "turn_end_reason": "completed"}})
    for index in range(600):
        store.messages.append({"thread_id": conversation.thread_id, "role": "user", "content": str(index)})
    before = store.storage.message_path(conversation.thread_id).read_bytes()
    # 缺索引先推进固定500物理行，禁止把未扫描区域当作不存在。
    assert not append(agent, conversation, "old", "error", "迟到旧稿")
    assert store.storage.message_path(conversation.thread_id).read_bytes() == before
    assert append(agent, conversation, "old", "error", "迟到旧稿")
    assert store.storage.message_path(conversation.thread_id).read_bytes() == before


def test_repair_queue_consumes_only_target_thread(runtime):
    from agent_py_agent.agent.gateway_parts.history_ledger import GatewayHistoryLedger

    agent, conversation = runtime
    other = agent.conversation_store.threads.create()
    for thread, content in [(other.thread_id, "其他线程"), (conversation.thread_id, "目标线程")]:
        history._queue_gateway_conversation_repair(
            agent, {}, SimpleNamespace(thread_id=thread), request_id=thread, role="assistant", content=content,
        )
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert not errors
    assert agent.conversation_store.messages.recent(other.thread_id) == []
    with GatewayHistoryLedger(agent.conversation_store).connection() as conn:
        assert conn.execute("SELECT thread FROM repairs").fetchall() == [(other.thread_id,)]
    assert [row.content for row in agent.conversation_store.messages.recent(conversation.thread_id)] == ["目标线程"]


def test_gateway_concurrent_append_exactly_once(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    agent, conversation = runtime
    barrier = Barrier(2)
    def run():
        barrier.wait()
        return append(agent, conversation, "same", "completed", "唯一终稿")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(lambda _: run(), range(2)))
    assert len(agent.conversation_store.messages.recent(conversation.thread_id)) == 1


def test_repair_append_committed_then_error_is_not_duplicated(runtime, monkeypatch):
    agent, conversation = runtime
    history._queue_gateway_conversation_repair(
        agent, {}, conversation, request_id="request", role="assistant", content="已落账",
        assistant_turn=history.GatewayAssistantTurn(end_reason="completed"),
    )
    original = agent.conversation_store.messages.append
    def fail_after_append(payload):
        original(payload)
        raise OSError("已落账后的故障")
    monkeypatch.setattr(agent.conversation_store.messages, "append", fail_after_append)
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert errors and "已落账后的故障" in str(errors)
    monkeypatch.setattr(agent.conversation_store.messages, "append", original)
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, [])
    assert len(agent.conversation_store.messages.recent(conversation.thread_id)) == 1


def test_large_legacy_queue_imports_bounded_then_replays_oldest_attempt(runtime):
    import json

    agent, conversation = runtime
    root = agent.conversation_store.storage.root / "message_repairs"
    root.mkdir()
    # 最早完成的记录故意放在文件名末尾，跨越迁移批次仍须先消费它。
    for index in range(1001):
        payload = {"thread_id": conversation.thread_id, "role": "assistant", "content": str(index),
                   "metadata": {"gateway_request_id": "request", "turn_end_reason": "completed",
                                "gateway_execution_attempt_id": str(index),
                                "gateway_repair_queued_at_ns": 1001 - index}}
        (root / f"{index:04}.json").write_text(json.dumps(payload))
    errors = []
    history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, errors)
    assert errors and "GATEWAY_REPAIR_MIGRATION_PENDING" in str(errors)
    assert agent.conversation_store.messages.recent(conversation.thread_id) == []
    for _ in range(5):
        resumed_errors = []
        history.repair_gateway_conversation_messages(agent.conversation_store, conversation.thread_id, resumed_errors)
        if not resumed_errors:
            break
        assert "GATEWAY_REPAIR_MIGRATION_PENDING" in str(resumed_errors)
    assert not resumed_errors
    assert [row.content for row in agent.conversation_store.messages.recent(conversation.thread_id)] == ["1000"]


def test_replaced_canonical_history_invalidates_dedupe_index(runtime):
    agent, conversation = runtime
    assert append(agent, conversation, "old", "completed", "旧终稿")
    assert append(agent, conversation, "old", "completed", "旧终稿")
    path = agent.conversation_store.storage.message_path(conversation.thread_id)
    path.unlink()
    assert append(agent, conversation, "new", "completed", "新历史")
    assert [row.content for row in agent.conversation_store.messages.recent(conversation.thread_id)] == ["新历史"]


def test_single_large_canonical_row_does_not_stall_index_forever(runtime):
    agent, conversation = runtime
    assert append(agent, conversation, "one", "completed", "答" * 400000)
    assert append(agent, conversation, "old", "error", "迟到旧稿")
    assert len(agent.conversation_store.messages.recent(conversation.thread_id)) == 1


def _unchanged_history_row(row, **_kwargs):
    return row


@pytest.mark.parametrize("attempts", [("old", "new"), ("one", "one"), ("", "")])
@pytest.mark.parametrize("native", ["none", "both", "failed_only"])
def test_failed_final_is_projection_only_superseded_by_success(runtime, attempts, native):
    from agent_py_agent.agent.conversation.history_display import (
        conversation_history_display_events,
    )
    from agent_py_agent.agent.conversation.history_seed import (
        freeze_history_source,
        history_source_text_messages,
    )
    from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows

    agent, conversation = runtime
    store = agent.conversation_store
    def row(content, part, reason, attempt):
        metadata = {"gateway_request_id": "request", "assistant_part_id": part,
                    "turn_end_reason": reason, "gateway_execution_attempt_id": attempt}
        if native != "none" and part == "final" and (native == "both" or reason == "error"):
            metadata.update(history.GatewayAssistantTurn(native_messages=[
                {"role": "assistant", "content": [{"type": "text", "text": content}]}
            ]).metadata())
        store.messages.append({"thread_id": conversation.thread_id, "role": "assistant",
                               "content": content, "metadata": metadata})
    row("失败片段", "final", "error", attempts[0])
    row("检查过程", "commentary:1", "", attempts[0])
    row("成功终稿", "final", "completed", attempts[1])
    rows = store.messages.recent(conversation.thread_id, limit=10)
    before = store.storage.message_path(conversation.thread_id).read_bytes()
    source = freeze_history_source(rows, project_row=_unchanged_history_row)
    assert history_source_text_messages(source) == (("assistant", "检查过程"), ("assistant", "成功终稿"))
    events = conversation_history_display_events(rows)
    texts = [event["payload"].get("text") for event in events]
    assert "失败片段" not in texts and "成功终稿" in texts and "检查过程" in texts
    provider = provider_history_messages_from_rows(rows)
    assert "失败片段" not in str(provider) and "成功终稿" in str(provider)
    assert store.storage.message_path(conversation.thread_id).read_bytes() == before


def test_failed_final_without_success_or_without_same_request_remains_visible(runtime):
    from agent_py_agent.agent.conversation.history_display import (
        conversation_history_display_events,
    )
    from agent_py_agent.agent.conversation.history_seed import (
        freeze_history_source,
        history_source_text_messages,
    )

    agent, conversation = runtime
    assert append(agent, conversation, "old", "error", "必须保留的失败")
    store = agent.conversation_store
    store.messages.append({"thread_id": conversation.thread_id, "role": "assistant", "content": "别的请求成功",
                           "metadata": {"gateway_request_id": "other", "turn_end_reason": "completed"}})
    rows = store.messages.recent(conversation.thread_id, limit=10)
    assert "必须保留的失败" in str(history_source_text_messages(freeze_history_source(rows, project_row=_unchanged_history_row)))
    assert "必须保留的失败" in str(conversation_history_display_events(rows))
    # 同页缺少成功行时不能越过分页边界猜测成功。
    assert "必须保留的失败" in str(conversation_history_display_events(rows[:1]))


@pytest.mark.parametrize("variation", ["different_thread", "commentary", "unknown", "background", "missing_request"])
def test_success_projection_does_not_hide_unrelated_or_unproven_rows(variation):
    from agent_py_agent.agent.conversation.native_history import (
        completed_gateway_final_attempts,
        superseded_gateway_final,
    )

    metadata = {"gateway_request_id": "r", "assistant_part_id": "final", "turn_end_reason": "error",
                "gateway_execution_attempt_id": "old"}
    failed = SimpleNamespace(thread_id="thread", role="assistant", metadata=metadata)
    completed = SimpleNamespace(thread_id="thread", role="assistant", metadata={
        "gateway_request_id": "r", "assistant_part_id": "final", "turn_end_reason": "completed",
        "gateway_execution_attempt_id": "new",
    })
    if variation == "different_thread":
        failed.thread_id = "other-thread"
    elif variation == "commentary":
        metadata["assistant_part_id"] = "commentary:1"
    elif variation == "unknown":
        metadata["turn_end_reason"] = ""
    elif variation == "background":
        metadata["background_delivery_reason"] = "child_completed"
    else:
        metadata.pop("gateway_request_id")
    assert not superseded_gateway_final(failed, completed_gateway_final_attempts([failed, completed]))
