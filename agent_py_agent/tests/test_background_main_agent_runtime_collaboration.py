from __future__ import annotations

import time

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


class _CapturingBackend:
    name = "capturing"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="后台主代理已检查任务树，并给出阶段汇报。", backend=self.name)


class _CollaborationRehearsalBackend:
    name = "collaboration-rehearsal"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "collaboration_case_closed" in prompt
            assert self.case_id in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            assert "artifact://source-a/e1" in prompt
            assert "artifact://source-b/e2" in prompt
            return ModelResponse(
                text='[TOOL_CALL]\n{"tool":"inspect_agent_tree"}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert '"does_not_dispatch": true' in prompt
        return ModelResponse(text="协作演练完成：已读取 case 状态和代理树。", backend=self.name)


class _BlockedCollaborationBackend:
    name = "blocked-collaboration"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "collaboration_case_closed" in prompt
            assert "不可达=1" in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        assert "collection_result" in prompt
        assert "ready_to_report" in prompt
        return ModelResponse(text="协作阻塞已确认：需要主代理调整策略。", backend=self.name)


class _PlainLanguageCollaborationBackend:
    name = "plain-language-collaboration"

    def __init__(self, *, case_id: str):
        self.case_id = case_id
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        if self.calls == 1:
            assert "帮我协调几个后台代理，有阻塞就继续安排或告诉我" in prompt
            return ModelResponse(
                text=f'[TOOL_CALL]\n{{"tool":"inspect_collaboration","case_id":"{self.case_id}"}}\n[/TOOL_CALL]',
                backend=self.name,
            )
        if self.calls == 2:
            assert "collection_result" in prompt
            assert "ready_to_report" in prompt
            return ModelResponse(
                text=(
                    '[TOOL_CALL]\n'
                    f'{{"tool":"update_collaboration","case_id":"{self.case_id}",'
                    '"status":"open","summary":"已看到阻塞请求，下一步需要换来源或补派代理。"}'
                    "\n[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        assert "open" in prompt
        return ModelResponse(text="我已经看到阻塞点，会换来源或补派代理继续推进。", backend=self.name)


class _SlowBackend:
    name = "slow"

    def __init__(self, *, sleep_seconds: float):
        self.sleep_seconds = sleep_seconds

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        time.sleep(self.sleep_seconds)
        return ModelResponse(text="后台主代理慢速检查完成。", backend=self.name)


def _runtime_parts(tmp_path, *, enable_tools: bool, backend=None):
    agent = SimpleAgent(AgentConfig(enable_tools=enable_tools, memory_path="memory.jsonl"), tmp_path)
    if backend is not None:
        agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'collaboration_store': agent.collaboration_store})
    return agent, store, channels, scheduler


def _thread_with_task(store: ConversationStore, *, title: str, goal: str, message: str = ""):
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'title': title, 'now': 1.0})
    if message:
        store.append_message({
            'thread_id': thread.thread_id,
            'role': "user",
            'content': message,
            'channel': "internal",
            'metadata': {"gateway_request_id": "task-1"},
            'now': 1.5,
        })
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': goal, 'now': 2.0})
    return thread


def _blocked_collaboration(agent: SimpleAgent, thread, *, source: str, target: str):
    from agent_py_agent.agent.collaboration import AgentCapability

    agent.collaboration_store.register_agent(AgentCapability(agent_id=target, capabilities=("query",)))
    case = agent.collaboration_store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "普通任务里的协作阻塞", 'summary': "响应者需要换来源。", 'priority': "normal", 'created_by': source, 'now': 3.0})
    request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': source, 'target_agent_ids': (target,), 'question': "请补充证据。", 'now': 4.0})
    agent.collaboration_store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "blocked", 'actor_agent_id': target, 'summary': "目标来源不可用。", 'now': 5.0})
    return case



def test_scheduler_processes_collaboration_cases_before_waking_agent(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability

    backend = _CapturingBackend()
    agent, _store, _channels, scheduler = _runtime_parts(tmp_path, enable_tools=False, backend=backend)
    thread = _thread_with_task(_store, title="协作任务", goal="多代理协作任务")
    agent.collaboration_store.register_agent(AgentCapability(agent_id="source-a", capabilities=("query",)))
    case = agent.collaboration_store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "需要主代理研判", 'summary': "协作证据已到位。", 'priority': "urgent", 'created_by': "source-a", 'now': 3.0})
    agent.collaboration_store.submit_evidence({'case_id': case.case_id, 'source_agent_id': "source-a", 'matched': True, 'summary': "证据到位。", 'evidence_refs': ("artifact://source-a/e1",), 'now': 4.0})

    reports = scheduler.tick(now=5.0)

    assert len(reports) == 1
    assert reports[0].reason == "collaboration_case_closed"  # 修 reason 透传后:报告带 signal 真实 reason(非泛泛 urgent_wake_signal)
    assert "collaboration_case_closed" in backend.prompts[0]
    assert "inspect_collaboration" in backend.prompts[0]
    assert agent.collaboration_store.load_case(case.case_id).status == "closed"


def test_offline_collaboration_rehearsal_uses_inspect_collaboration_and_tree_tools(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability

    agent, _store, channels, scheduler = _runtime_parts(tmp_path, enable_tools=True)
    thread = _thread_with_task(
        _store,
        title="离线协作演练",
        goal="通用协作演练",
        message="发现需要多代理协作的事情后，请自己看证据和代理树，再决定是否汇报。",
    )
    for capability in (
        AgentCapability(agent_id="source-a", capabilities=("query", "summarize"), status="available"),
        AgentCapability(agent_id="source-b", capabilities=("query", "correlate"), status="available"),
    ):
        agent.collaboration_store.register_agent(capability)
    case = agent.collaboration_store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "需要跨源协作判断", 'summary': "两个来源都可能有相关证据，需要主代理读取 case 后判断。", 'priority': "urgent", 'created_by': "source-a", 'required_capabilities': ("query",), 'entities': {"entity": "open-world-signal"}, 'now': 3.0})
    request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-b",), 'required_capabilities': ("query",), 'question': "请围绕同一实体补充证据。", 'now': 4.0})
    _submit_rehearsal_evidence(agent, case.case_id, request.request_id)
    backend = _CollaborationRehearsalBackend(case_id=case.case_id)
    agent.backend = backend

    reports = scheduler.tick(now=7.0)

    assert len(reports) == 1
    assert reports[0].response == "协作演练完成：已读取 case 状态和代理树。"
    assert backend.calls == 3
    sent = channels.adapter("internal").sent_messages
    assert sent[0].content == "协作演练完成：已读取 case 状态和代理树。"
    assert agent.collaboration_store.case_status(case.case_id)["evidence_count"] == 2


def _submit_rehearsal_evidence(agent: SimpleAgent, case_id: str, request_id: str) -> None:
    for source, ref, confidence, now in (
        ("source-a", "artifact://source-a/e1", 0.8, 5.0),
        ("source-b", "artifact://source-b/e2", 0.75, 6.0),
    ):
        agent.collaboration_store.submit_evidence({'case_id': case_id, 'request_id': request_id, 'source_agent_id': source, 'matched': True, 'summary': f"来源 {source[-1].upper()} 找到补充证据。", 'evidence_refs': (ref,), 'confidence': confidence, 'now': now})


def test_long_running_watcher_wakes_main_agent_when_responder_blocks(tmp_path) -> None:
    agent, _store, _channels, scheduler = _runtime_parts(tmp_path, enable_tools=True)
    thread = _thread_with_task(
        _store,
        title="常驻协作任务",
        goal="长期 watcher 协作",
        message="后台持续观察，有异常或协作阻塞时叫醒主代理判断。",
    )
    case = _watcher_blocked_case(agent, thread)
    backend = _BlockedCollaborationBackend(case_id=case.case_id)
    agent.backend = backend

    reports = scheduler.tick(now=6.0)

    assert len(reports) == 1
    assert reports[0].reason == "collaboration_case_closed"  # 修 reason 透传后:报告带 signal 真实 reason(非泛泛 wake_signal)
    assert reports[0].response == "协作阻塞已确认：需要主代理调整策略。"
    assert backend.calls == 2
    assert agent.collaboration_store.load_case(case.case_id).status == "closed"


def _watcher_blocked_case(agent: SimpleAgent, thread):
    from agent_py_agent.agent.collaboration import AgentCapability

    agent.collaboration_store.register_agent(
        AgentCapability(agent_id="watcher-a", capabilities=("observe", "query"), status="available")
    )
    agent.collaboration_store.register_agent(
        AgentCapability(agent_id="watcher-b", capabilities=("query", "correlate"), status="available")
    )
    case = agent.collaboration_store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': "常驻观察发现阻塞", 'summary': "一个响应者无法访问来源，需要主代理决定换来源或降级处理。", 'priority': "normal", 'created_by': "watcher-a", 'required_capabilities': ("query",), 'now': 3.0})
    request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "watcher-a", 'target_agent_ids': ("watcher-b",), 'required_capabilities': ("query",), 'question': "请围绕同一实体补充来源。", 'now': 4.0})
    agent.collaboration_store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "blocked", 'actor_agent_id': "watcher-b", 'summary': "来源连接失败，需要主代理决定是否改查其他来源。", 'now': 5.0})
    return case


def test_scheduler_batches_multiple_wake_signals_for_same_thread(tmp_path) -> None:
    from agent_py_agent.agent.collaboration import AgentCapability

    backend = _CapturingBackend()
    agent, store, _channels, scheduler = _runtime_parts(tmp_path, enable_tools=False, backend=backend)
    thread = _thread_with_task(store, title="多事件同线程", goal="多 case 协作")
    agent.collaboration_store.register_agent(AgentCapability(agent_id="source-b", capabilities=("query",)))
    for index in range(2):
        case = agent.collaboration_store.open_case({'thread_id': thread.thread_id, 'task_id': "task-1", 'title': f"阻塞 case {index}", 'priority': "normal", 'created_by': "source-a", 'now': 3.0 + index})
        request = agent.collaboration_store.request_collaboration({'case_id': case.case_id, 'requester_agent_id': "source-a", 'target_agent_ids': ("source-b",), 'question': "请补充证据。", 'now': 4.0 + index})
        agent.collaboration_store.update_request_status({'case_id': case.case_id, 'request_id': request.request_id, 'status': "blocked", 'actor_agent_id': "source-b", 'summary': "来源暂不可用。", 'now': 5.0 + index})

    reports = scheduler.tick(now=10.0)

    assert len(reports) == 1
    assert len(backend.prompts) == 1
    assert store.pending_wake_signals() == []


def test_plain_language_background_scenario_can_rework_blocked_collaboration(tmp_path) -> None:
    agent, _store, _channels, scheduler = _runtime_parts(tmp_path, enable_tools=True)
    thread = _thread_with_task(
        _store,
        title="普通自然语言协作",
        goal="普通自然语言协作任务",
        message="帮我协调几个后台代理，有阻塞就继续安排或告诉我。",
    )
    case = _blocked_collaboration(agent, thread, source="agent-a", target="agent-b")
    backend = _PlainLanguageCollaborationBackend(case_id=case.case_id)
    agent.backend = backend

    reports = scheduler.tick(now=6.0)

    assert len(reports) == 1
    assert reports[0].response.startswith(
        "我已经看到阻塞点，会换来源或补派代理继续推进。\n\n操作核验（以程序记录为准）："
    )
    public_verification = _store.recent_messages(thread.thread_id, limit=1)[0].metadata[
        "operation_verification"
    ]
    assert public_verification["status"] == "succeeded"
    assert [
        (item["tool"], item["status"]) for item in public_verification["groups"]
    ] == [("update_collaboration", "succeeded")]
    updated_case = agent.collaboration_store.load_case(case.case_id)
    assert updated_case.status == "open"
    assert "raw_case_status" not in updated_case.metadata
    assert "case_status_protocol_error" not in updated_case.metadata
