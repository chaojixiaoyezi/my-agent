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


@pytest.mark.parametrize(('historical_same_request', 'cancel_before_requeue', 'named_audit'),
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)])
def test_cold_gateway_index_defers_same_request_then_executes_once(tmp_path, monkeypatch, historical_same_request, cancel_before_requeue, named_audit):
    import json
    import time

    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import (
        GatewayAskParams,
        _process_gateway_requests,
        gateway_paths,
        submit_gateway_ask,
    )
    from agent_py_agent.agent.gateway_parts.history_ledger import GatewayHistoryLedger
    from agent_py_agent.agent.gateway_parts.request_worker import (
        _pending_request_entry_sort_key,
        _PendingGatewayRequest,
    )
    from agent_py_agent.agent.settings.config import AgentConfig

    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'), prompt_files=[],
                                   gateway_per_user_owner_scoping=False), tmp_path / 'workspace')
    agent.backend = get_backend('echo', agent.config)
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    params = GatewayAskParams(prompt='开头', save=False, chat_session_id='cold-index', agent=agent)
    _, _, response = submit_gateway_ask(paths, params=params)
    _process_gateway_requests(agent, paths)
    assert json.loads(response.read_text())['ok']
    store = agent.conversation_store
    thread = store.threads.resolve(channel='chat', channel_conversation_id='cold-index', channel_user_id='local-agent')
    system_task = {'kind': 'audit_prepare', 'attributes': {'conversation_audit_prepare': True,
        'conversation_cancellation_scope': 'foreground', 'conversation_work_kind': 'audit',
        'conversation_work_name': '冷索引准备'}} if named_audit else None
    request_id, request_path, response = submit_gateway_ask(paths, params=GatewayAskParams(
        prompt='只回复一次', save=False, chat_session_id='cold-index', agent=agent, system_task=system_task))
    if named_audit:
        from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
        update_json_file_atomic(request_path, lambda value: {**value, 'conversation_audit_scope': {'audit_id': 'forged'}},
                                require_existing=True)
    for index in range(1200):
        store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': f'旧消息 {index}',
            'metadata': {'gateway_request_id': request_id if historical_same_request and index == 1199 else f'old-{index}'}})
    ledger = GatewayHistoryLedger(store)
    for suffix in ('', '-wal', '-shm'):
        type(ledger.path)(str(ledger.path) + suffix).unlink(missing_ok=True)
    calls = []
    original = agent.run
    def observed(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(agent, 'run', observed)
    steps = []
    actual_catch_up = GatewayHistoryLedger.catch_up
    def measured_catch_up(*args, **kwargs):
        steps.append(bool(calls))
        return actual_catch_up(*args, **kwargs)
    monkeypatch.setattr(GatewayHistoryLedger, 'catch_up', measured_catch_up)
    if cancel_before_requeue:
        from agent_py_agent.agent.gateway_parts import request_worker
        from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
        actual_requeue = request_worker._requeue_history_preparation
        def stop_then_requeue(context, paths, pending):
            update_json_file_atomic(context.processing_path,
                lambda value: {**value, 'cancel_requested': True, 'turn_phase': 'closing'}, require_existing=True)
            return actual_requeue(context, paths, pending)
        monkeypatch.setattr(request_worker, '_requeue_history_preparation', stop_then_requeue)
    deferred = 0
    for _ in range(6):
        step_start = len(steps)
        _process_gateway_requests(agent, paths)
        if response.exists():
            break
        assert steps[step_start:] == [False], '每个准备片只准一次索引事务'
        pending = json.loads((paths.inbox / (request_id + '.json')).read_text())
        assert pending['status'] == 'pending' and pending['id'] == request_id
        assert pending['deferred_reason'] == 'GATEWAY_HISTORY_INDEX_PENDING'
        assert _pending_request_entry_sort_key(_PendingGatewayRequest(paths.inbox / (request_id + '.json'), pending))[0] == 1
        assert not calls and not (paths.failed / (request_id + '.json')).exists()
        if named_audit:
            assert not [link for link in store.tasks.list(thread.thread_id) if link.work_name == '冷索引准备']
        deferred += 1
        time.sleep(0.11)
    result = json.loads(response.read_text())
    if cancel_before_requeue:
        assert result['status'] == 'interrupted', result
        assert not calls and not (paths.processing / (request_id + '.json')).exists()
        return
    assert result['ok'], result
    assert deferred >= 2 and len(calls) == 1
    assert steps[step_start:].count(False) == (2 if named_audit else 1), '仅命名任务写入前允许额外隔离预检'
    _process_gateway_requests(agent, paths)
    assert len(calls) == 1
    rows = [json.loads(line) for line in store.storage.message_path(thread.thread_id).read_text().splitlines()]
    matched = [row for row in rows if row['role'] == 'user' and row.get('metadata', {}).get('gateway_request_id') == request_id]
    assert len(matched) == 1
    if named_audit:
        links = [link for link in store.tasks.list(thread.thread_id) if link.work_name == '冷索引准备']
        assert len(links) == 1
        assert matched[0]['metadata']['conversation_task_id'] == links[0].task_id != 'forged'


@pytest.mark.parametrize('failure', ['truncated', 'oversized', 'io', 'stalled'])
def test_index_preparation_never_defers_real_failure_or_no_progress(runtime, monkeypatch, failure):
    from agent_py_agent.agent.gateway_parts.history_ledger import (
        GatewayHistoryLedger,
        HistoryIndexPending,
    )
    from agent_py_agent.agent.gateway_parts.request_errors import GatewayHistoryPreparationPending
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    agent, conversation = runtime
    if failure in ('truncated', 'oversized'):
        def damaged(*args, **kwargs):
            if failure == 'truncated':
                raise DataCorruptionError('GATEWAY_HISTORY_TRUNCATED: no complete canonical row')
            raise HistoryIndexPending('GATEWAY_HISTORY_ROW_EXCEEDS_BUDGET: unchanged cursor, no append')
        monkeypatch.setattr(GatewayHistoryLedger, 'catch_up', damaged)
    elif failure == 'io':
        def broken(*args):
            raise OSError('synthetic I/O failure')
        monkeypatch.setattr(GatewayHistoryLedger, '_snapshot', broken)
    else:
        monkeypatch.setattr(GatewayHistoryLedger, 'catch_up', lambda *a, **k: False)
    with pytest.raises((DataCorruptionError, OSError, HistoryIndexPending)) as caught:
        history.append_gateway_conversation_message(agent, {'id': 'request', 'execution_attempt_id': 'attempt', 'lease_epoch': 1},
            conversation, request_id='request', role='user', content='尚未执行', defer_index_pending=True)
    assert type(caught.value) is not GatewayHistoryPreparationPending
