"""后台接收本人输入后的回复义务沿实际 ACK 和 Compact 携带。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime import guidance
from agent_py_agent.agent.conversation.active_turn_input import merge_active_turn_user_inputs
from agent_py_agent.agent.conversation.background_execution import (
    collect_background_execution_result,
)
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_delivery_decision,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


@pytest.mark.parametrize("origin,consume,accepted", [("", True, True), ("session_message", True, False), ("", False, False)])
def test_only_consumed_direct_user_input_establishes_reply(tmp_path, origin, consume, accepted):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.conversation_store.guidance.append({
        "target_type": "agent_run", "target_id": "main-run", "message": "解释当前进展",
        "now": 10.0, "metadata": {"origin_kind": origin, "kind": "active_turn_user_input"},
    })
    params = _tool_loop_params(run_id="main-run")
    assert guidance.inject_pending_guidance(agent, params, now=11.0)
    assert not any(p.get("user_reply_accepted") for p in params.active_turn_user_inputs)
    if consume:
        assert guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id="reply-test") == 1
        assert guidance.acknowledge_injected_turn_input(agent, params, now=12.0) == 1
    result = collect_background_execution_result(agent, SimpleNamespace(
        response="当前进展", active_turn_user_inputs=params.active_turn_user_inputs), {})
    assert result.user_reply_accepted is accepted


def test_compact_merge_preserves_acceptance_without_allowing_content_change():
    packet = {"schema_version": "active-turn-user-input.v1", "input_ids": ["guidance-1"], "text": "当前进展"}
    accepted = dict(packet, user_reply_accepted=True)
    for first, second in [(packet, accepted), (accepted, packet)]:
        merged = merge_active_turn_user_inputs([first], [second])
        assert merged == [accepted]
        with pytest.raises(ValueError, match="冲突"):
            merge_active_turn_user_inputs(merged, [dict(packet, text="不同内容")])


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("task_status", ["active", "cancelled"])
def test_lifecycle_reply_obligation_respects_cancellation(tmp_path, accepted, task_status):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.create()
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-root", "status": task_status})
    store.goals.create({"thread_id": thread.thread_id, "task_id": "task-root", "objective": "继续任务"})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="task-root",
        reason="subagent_runner_finished", wake_signal={"root_task_id": "task-root"})
    expected = ((False, "task_cancelled") if task_status == "cancelled" else
                (True, "active_turn_user_reply") if accepted else
                (False, "thread_goal_lifecycle_internal"))
    assert _background_delivery_decision(agent, request, store=store, user_reply_accepted=accepted) == expected


@pytest.mark.parametrize("accepted", [False, True])
def test_direct_reply_bypasses_external_idle_silence(tmp_path, accepted):
    from agent_py_agent.agent.conversation import runtime
    from agent_py_agent.agent.conversation.goal_progress_fuse import (
        WAKE_SNAPSHOT_KEY,
        goal_progress_snapshot,
    )

    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    store = agent.conversation_store
    thread = store.threads.create()
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "root", "status": "active"})
    goal = store.goals.create({"thread_id": thread.thread_id, "task_id": "root", "objective": "持续检查"})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id="root", reason="thread_goal_continue",
        wake_signal={"metadata": {WAKE_SNAPSHOT_KEY: goal_progress_snapshot(goal, "active")}})
    result = SimpleNamespace(user_reply_accepted=accepted, tool_call_count=0, material_progress_count=0)
    assert runtime._background_goal_delivery_mode(goal, request, result, agent=agent, store=store,
        deliver=True, delivery_reason="thread_goal_progress", route_ownership=runtime.ROUTE_EXTERNAL) == (
            (True, "thread_goal_progress", False) if accepted else (False, "thread_goal_idle", True))


def test_run_once_publishes_accepted_reply_once(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime

    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=False), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "internal",
        "channel_conversation_id": "direct-reply", "channel_user_id": "owner"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "root", "status": "active"})
    store.goals.create({"thread_id": thread.thread_id, "task_id": "root", "objective": "继续检查"})
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    packet = {"schema_version": "active-turn-user-input.v1", "input_ids": ["accepted"],
              "text": "当前进展", "user_reply_accepted": True}
    monkeypatch.setattr(runtime, "_run_agent", lambda *a, **kw: collect_background_execution_result(
        agent, SimpleNamespace(response="已核对当前进展。", active_turn_user_inputs=[packet]), {}))
    signal = store.wakes.raise_signal({"thread_id": thread.thread_id, "root_task_id": "root",
        "reason": "subagent_runner_finished"})
    params = {"thread_id": thread.thread_id, "task_id": "root", "reason": "subagent_runner_finished",
              "wake_signal": signal.to_dict()}
    report = runtime.run_once(params)
    assert report.delivery_reason == "active_turn_user_reply"
    assert report.delivery_status == "sent"
    runtime.run_once(params)
    rows = store.messages.recent(thread.thread_id, limit=20)
    assert [row.content for row in rows if row.role == "assistant" and row.content] == ["已核对当前进展。"]


@pytest.mark.parametrize('metadata', [{}, {'kind': 'goal_update'}, {'kind': 'plugin_interjection'},
    {'kind': 'active_turn_user_input', 'origin_kind': 'session_task'},
    {'kind': 'active_turn_user_input', 'origin_kind': 'unknown'}])
def test_internal_guidance_never_creates_public_reply_debt(tmp_path, metadata):
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    agent.conversation_store.guidance.append({'target_type': 'agent_run', 'target_id': 'main-run',
        'message': '内部状态', 'metadata': metadata})
    params = _tool_loop_params(run_id='main-run')
    assert guidance.inject_pending_guidance(agent, params)
    guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id='internal')
    guidance.acknowledge_injected_turn_input(agent, params)
    assert not guidance.active_turn_user_reply_required(params)
    assert not any(packet.get('user_reply_accepted') for packet in params.active_turn_user_inputs)


def _consume_durable_input(agent, thread_id, *, key='input-1', task_id='root'):
    store = agent.conversation_store
    store.guidance.append_once({'target_type': 'task', 'target_id': task_id, 'message': '私人输入不可复制到义务表',
        'metadata': {'kind': 'active_turn_user_input', 'thread_id': thread_id, 'expected_turn_id': 'req-1'}},
        dedupe_key=key)
    params = _tool_loop_params(task_id=task_id)
    assert guidance.inject_pending_guidance(agent, params)
    guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id=key)
    guidance.acknowledge_injected_turn_input(agent, params)
    return params


def test_consumed_input_survives_failed_slice_and_compact_state_loss(tmp_path):
    from agent_py_agent.agent.conversation.guidance_reply_obligations import pending, settle
    from agent_py_agent.agent.conversation.store import ConversationStore
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    thread = agent.conversation_store.threads.create()
    params = _consume_durable_input(agent, thread.thread_id)
    original = pending(agent.conversation_store.storage, thread.thread_id, 'root')
    assert len(original) == 1
    # 失败/Compact丢弃原params及其临时标志；新Store只读取持久状态。
    params.live_archive_state.clear()
    restored = ConversationStore(agent.conversation_store.storage.root)
    assert pending(restored.storage, thread.thread_id, 'root') == original
    assert pending(restored.storage, 'other-thread', 'root') == ()
    assert pending(restored.storage, thread.thread_id, 'other-task') == ()
    assert '私人输入'.encode() not in restored.storage.guidance_reply_obligations_path.read_bytes()
    settle(restored.storage, thread.thread_id, 'root', original)
    assert pending(restored.storage, thread.thread_id, 'root') == ()
    # 原ACK补账不得重新建立已完成回复的义务。
    with restored.guidance.ledger.turn_guard('req-1'):
        assert restored.guidance.acknowledgements.repair_locked('req-1') == 0
    assert pending(restored.storage, thread.thread_id, 'root') == ()


def test_settlement_only_clears_exact_delivered_input_ids(tmp_path):
    from agent_py_agent.agent.conversation.guidance_reply_obligations import pending, settle
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    thread = agent.conversation_store.threads.create()
    _consume_durable_input(agent, thread.thread_id)
    first = pending(agent.conversation_store.storage, thread.thread_id, 'root')
    _consume_durable_input(agent, thread.thread_id, key='input-2')
    settle(agent.conversation_store.storage, thread.thread_id, 'root', first)
    remaining = pending(agent.conversation_store.storage, thread.thread_id, 'root')
    assert len(remaining) == 1 and remaining != first


def test_btw_stays_internal_even_when_user_input_is_consumed(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    store = agent.conversation_store
    thread = store.threads.create()
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'root', 'status': 'active'})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id='root', reason='user_guidance')
    assert _background_delivery_decision(agent, request, store=store, user_reply_accepted=True) == (
        False, 'user_guidance_applied_internal')


@pytest.mark.parametrize("cancel_before_send", [False, True])
def test_provider_failure_after_ack_retries_through_real_background_loop(tmp_path, monkeypatch, cancel_before_send):
    from agent_py_agent.agent.agent_core._tool_loop_service import execute_tool_loop
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation.guidance_reply_obligations import pending
    from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime

    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True), tmp_path)
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id': 'owner', 'channel': 'internal',
        'channel_conversation_id': 'retry-reply', 'channel_user_id': 'owner'})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'root', 'status': 'active'})
    store.goals.create({'thread_id': thread.thread_id, 'task_id': 'root', 'objective': '继续检查'})
    store.guidance.append_once({'target_type': 'task', 'target_id': 'root', 'message': '请解释进展',
        'metadata': {'kind': 'active_turn_user_input', 'thread_id': thread.thread_id,
                     'expected_turn_id': 'req-1'}}, dedupe_key='failed-slice-input')

    class FailsAfterAcceptance:
        name = 'fails_after_acceptance'
        calls = 0
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'probe-call', 'name': 'read_file', 'input': {'path': 'README.md'}}])
            raise RuntimeError('provider disconnected after accepted input')

    agent.backend = FailsAfterAcceptance()
    with pytest.raises(RuntimeError, match='provider disconnected'):
        execute_tool_loop(agent, _tool_loop_params(task_id='root', allowed_tools=[]))
    assert agent.backend.calls >= 2
    assert pending(store.storage, thread.thread_id, 'root')

    from agent_py_agent.agent.backends.base import EchoBackend

    class SuccessfulRetry(EchoBackend):
        name = 'successful_retry'
        def generate(self, prompt, on_chunk=None, **kwargs):
            return ModelResponse(text='已核对当前进展。', backend=self.name)

    agent.backend = SuccessfulRetry()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store)
    if cancel_before_send:
        from agent_py_agent.agent.conversation import runtime as runtime_module
        decide = runtime_module._background_delivery_decision
        def cancel_after_decision(*args, **kwargs):
            decision = decide(*args, **kwargs)
            store.tasks.update_status({'task_id': 'root', 'status': 'cancelled'})
            return decision
        monkeypatch.setattr(runtime_module, '_background_delivery_decision', cancel_after_decision)
    signal = store.wakes.raise_signal({'thread_id': thread.thread_id, 'root_task_id': 'root',
                                       'reason': 'subagent_runner_finished'})
    report = runtime.run_once({'thread_id': thread.thread_id, 'task_id': 'root',
                              'reason': 'subagent_runner_finished', 'wake_signal': signal.to_dict()})
    assert report.delivery_reason == 'active_turn_user_reply'
    assert report.delivery_status == ('suppressed' if cancel_before_send else 'sent')
    assert bool(pending(store.storage, thread.thread_id, 'root')) is cancel_before_send
    if cancel_before_send:
        assert not [row for row in store.messages.recent(thread.thread_id) if row.role == 'assistant' and row.content]


def test_ack_crash_does_not_publish_before_consumption_projection(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.guidance_reply_obligations import pending
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    store = agent.conversation_store
    thread = store.threads.create()
    store.guidance.append_once({'target_type': 'task', 'target_id': 'root', 'message': '补充',
        'metadata': {'kind': 'active_turn_user_input', 'thread_id': thread.thread_id,
                     'expected_turn_id': 'req-1'}}, dedupe_key='crash-input')
    params = _tool_loop_params(task_id='root')
    assert guidance.inject_pending_guidance(agent, params)
    guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id='crash-call')
    write_receipt = store.guidance.ledger.write_receipt
    def crash_before_consumed(path, payload, **kwargs):
        if payload.get('status') == 'consumed':
            raise RuntimeError('simulated crash after obligation insert')
        return write_receipt(path, payload, **kwargs)
    monkeypatch.setattr(store.guidance.ledger, 'write_receipt', crash_before_consumed)
    with pytest.raises(RuntimeError, match='simulated crash'):
        guidance.acknowledge_injected_turn_input(agent, params)
    assert pending(store.storage, thread.thread_id, 'root') == ()
    monkeypatch.setattr(store.guidance.ledger, 'write_receipt', write_receipt)
    with store.guidance.ledger.turn_guard('req-1'):
        assert store.guidance.acknowledgements.repair_locked('req-1') == 0
    assert len(pending(store.storage, thread.thread_id, 'root')) == 1


def test_child_completion_tool_reply_is_mirrored_without_second_send(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime
    from agent_py_agent.tests.test_background_main_agent_runtime import FakeDeliveryService
    from agent_py_agent.tests.test_required_notice_reply_slot import reply_slot_case

    case = reply_slot_case(tmp_path, monkeypatch, notice=False, message='已经核对，继续处理。')
    case.store.tasks.bind({'thread_id': case.thread.thread_id, 'task_id': 'audit-1',
                          'goal': '继续处理'}, host_channel_actor=case.actor)
    case.store.goals.create({'thread_id': case.thread.thread_id, 'task_id': 'audit-1', 'objective': '继续处理'})
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=case.agent, store=case.store, channels=channels)
    report = runtime.run_once({'thread_id': case.thread.thread_id, 'task_id': 'audit-1',
        'reason': 'subagent_runner_finished', 'wake_signal': {'wake_signal_id': 'wake-child',
        'root_task_id': 'audit-1'}, 'now': case.now})
    assert report.delivery_status == 'sent'
    assert len(case.sends) == 1
    assert channels.adapter('feishu').sent_messages == []
    rows = case.store.messages.recent(case.thread.thread_id, limit=20)
    assert [row.content for row in rows if row.role == 'assistant' and row.content] == ['已经核对，继续处理。']


def test_eight_generation_compact_yield_keeps_consumed_input_obligation(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import background_execution
    from agent_py_agent.agent.conversation.guidance_reply_obligations import pending
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.tests.test_background_main_agent_runtime import (
        test_background_compact_slice_yields_after_eight_progressful_generations as compact_case,
    )

    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path / 'owner')
    from agent_py_agent.agent.conversation.models import ConversationThread

    thread = ConversationThread(thread_id='thread-yield', canonical_user_id='owner-yield')
    agent.conversation_store.threads.write(thread)
    attempts = []
    run_attempt = background_execution._run_background_recovery_attempt
    def consume_then_run(*args, **kwargs):
        if not attempts:
            _consume_durable_input(agent, thread.thread_id, task_id='task-yield')
        attempts.append(True)
        return run_attempt(*args, **kwargs)
    monkeypatch.setattr(background_execution, '_run_background_recovery_attempt', consume_then_run)
    # 原八代fixture实际调用公共run_background_turn_with_compact，并验证typed yield。
    # 每代结果不携带active_turn_user_inputs，因此不能靠临时包幸存来通过。
    compact_case(monkeypatch, tmp_path / 'compact', False)
    assert len(attempts) == 9
    restored = ConversationStore(agent.conversation_store.storage.root)
    assert len(pending(restored.storage, thread.thread_id, 'task-yield')) == 1
    assert pending(restored.storage, thread.thread_id, 'new-task') == ()


def test_pending_reply_query_is_scoped_to_task_and_uses_only_frozen_ids(tmp_path):
    from agent_py_agent.agent.conversation.guidance_reply_obligations import pending, settle
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    thread = agent.conversation_store.threads.create()
    _consume_durable_input(agent, thread.thread_id)
    frozen = pending(agent.conversation_store.storage, thread.thread_id, 'root')
    assert pending(agent.conversation_store.storage, thread.thread_id, 'new-task') == ()
    # 错线程/新任务既不能获取，也不能借旧ID结清原任务。
    settle(agent.conversation_store.storage, thread.thread_id, 'new-task', frozen)
    settle(agent.conversation_store.storage, 'other-thread', 'root', frozen)
    assert pending(agent.conversation_store.storage, thread.thread_id, 'root') == frozen


def test_technical_continuation_uses_current_child_state_and_allows_no_children(tmp_path):
    from agent_py_agent.agent.subagents.models import TaskStatus
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    store = agent.conversation_store
    thread = store.threads.create()
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id='no-children',
                                   reason='technical_cutoff_continue')
    assert _background_delivery_decision(agent, request, store=store) == (True, 'technical_continuation_completion')
    root = agent.subagents.create_run(goal='主任务', thought='执行', plan=['执行'], role='coordinator')
    child = agent.subagents.create_run(goal='子任务', thought='执行', plan=['执行'], role='researcher',
                                       parent_id=root.id, root_id=root.id)
    child.status = TaskStatus.RUNNING.value
    agent.subagents.save(child)
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id=root.id,
                                   reason='technical_cutoff_continue')
    assert _background_delivery_decision(agent, request, store=store, user_reply_accepted=True) == (
        False, 'technical_continuation_children_active')
    child.status = TaskStatus.DONE.value
    agent.subagents.save(child)
    assert _background_delivery_decision(agent, request, store=store) == (True, 'technical_continuation_completion')
