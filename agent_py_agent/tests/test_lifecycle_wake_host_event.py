"""生命周期唤醒片记为宿主事件，不再写第二条用户任务（2026-09-28，T3 验收观察 2 的 A）。

背景：子代理阻塞/完成后，父代理的唤醒片把原任务再次渲染成 "# User Task"，成了"最新的用户消息"；推荐节按原任务文字召回，
第一个就是 create_subagents；约 3 万字的后台上下文注入写进会话历史，之后每一轮都重放。
锁定：
- 唤醒事实只从唤醒信封的结构化字段确定性投影（白名单、键排序、有界），不收原始结果 JSON；
- 原生 IR 当前回合以宿主事件（RuntimeFactsTurn，来源 host.lifecycle_wake）开头，文本协议与缓存布局用 "# Host Event"，
  首行固定；普通回合字节不变；
- 推荐节按触发类型给固定短名单，list_agents 在第一位，只列本轮可见工具；
- 唤醒片保存到会话历史时去掉本片的后台上下文注入，宿主事件与工具往返照常保存；
- 脚本化假模型（按"最新 User Task"行事）走真实后台链路时不再重复派工；"一律派工"的变体被一次性编排去重拦下，
  根任务的子代理始终只有一个。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams, RuntimeLoopParams
from agent_py_agent.agent.agent_core.runtime.loop_support import (
    ToolSectionsRequest,
    _completed_turn_native_messages,
    _current_turn_opener_count,
    _native_initial_tool_ir_history,
    _resolve_tool_sections,
)
from agent_py_agent.agent.agent_core.runtime.turn_trigger import (
    HOST_EVENT_FACTS_SOURCE,
    HOST_EVENT_FIRST_LINE,
    HOST_EVENT_FIRST_LINE_ORIGIN_TASK,
    SESSION_TASK_FACTS_SOURCE,
    SESSION_TASK_FIRST_LINE,
    TURN_TRIGGER_SESSION_TASK,
    TurnTrigger,
    current_turn_text,
    session_task_trigger,
    turn_trigger_recommendation,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.tool_ir import (
    AssistantTurn,
    CompactionSummary,
    RuntimeFactsTurn,
    UserTurn,
)
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    ConversationStore,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.lifecycle_wake_event import lifecycle_wake_turn_trigger
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _background_model_inputs,
    _goal_runtime_context,
    _run_params,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive.tool_output_externalizer import (
    ExternalizeToolOutputRequest,
    externalize_tool_output_record,
)
from agent_py_agent.agent.prompting_parts.builder import (
    PromptBuildRequest,
    ToolSections,
    render_prepared_prompt,
)
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolProtocolSnapshot,
)

_CHILD_WAKE = "subagent_runner_finished"
_REQUEST = "gwreq-foreground-1"
_TASK = "task-root"
_OBJECTIVE = "AC-S2 请派一个子代理去读父项目目录里的资料并写出要点。"
_CHILD_GOAL = "读取父项目资料并写出三条要点"


# 函数用途: 一条子代理阻塞唤醒的信封，字段与宿主真实写入的同名。
def _wake(**metadata: object) -> dict:
    return {
        "wake_signal_id": "wake-1", "reason": _CHILD_WAKE, "source_agent_id": "subagent-1", "root_task_id": _TASK,
        "dedupe_key": "subagent-finished:subagent-1:BLOCKED", "evidence_refs": ["ref-a"],
        "metadata": {
            "task_id": "subagent-1", "status": "BLOCKED", "turn_end_reason": "tool_failure_halt",
            "conversation_request_id": _REQUEST, "runner_result_json": "{\"raw\": \"x\"}",
            "output_json": "{\"raw\": \"y\"}", **metadata,
        },
    }


def _trigger(**metadata: object):
    return lifecycle_wake_turn_trigger(_CHILD_WAKE, _wake(**metadata), [_REQUEST])


def test_wake_facts_are_a_bounded_deterministic_projection() -> None:
    trigger = _trigger(completion_message="很" * 700, artifact_refs=[f"ref-{index}" for index in range(12)],
                       tool_failure_halt={"code": "AUTH", "tool": "read_file", "count": 3})
    reordered = lifecycle_wake_turn_trigger(_CHILD_WAKE, dict(reversed(list(_wake(
        completion_message="很" * 700, artifact_refs=[f"ref-{index}" for index in range(12)],
        tool_failure_halt={"count": 3, "tool": "read_file", "code": "AUTH"}).items()))), [_REQUEST])
    facts = json.loads(trigger.event_facts)

    assert trigger.event_facts == reordered.event_facts
    assert (trigger.kind, trigger.reason, trigger.wake_signal_id, trigger.source_agent_id, trigger.origin_request_ids) == (
        "lifecycle_wake", _CHILD_WAKE, "wake-1", "subagent-1", (_REQUEST,))
    assert facts["origin_request_ids"] == [_REQUEST] and facts["root_task_id"] == _TASK
    child = facts["child"]
    assert (child["status"], child["turn_end_reason"]) == ("BLOCKED", "tool_failure_halt")
    assert child["tool_failure_halt"] == {"code": "AUTH", "count": 3, "tool": "read_file"}
    assert child["completion_message"].endswith("…（已截断）") and len(child["completion_message"]) < 700
    assert len(child["artifact_refs"]) == 9 and child["artifact_refs"][-1] == "…（其余已省略）"
    # 原始结果 JSON、去重键和证据引用这类宿主内部字段不进宿主事件。
    for hidden in ("runner_result_json", "output_json", "dedupe_key", "evidence_refs", "raw"):
        assert hidden not in trigger.event_facts
    assert current_turn_text("原任务", trigger).splitlines()[:2] == ["# Host Event", HOST_EVENT_FIRST_LINE]
    assert current_turn_text("原任务", None) == "# User Task\n原任务"


def test_origin_task_is_attached_only_when_the_caller_passes_one() -> None:
    named = lifecycle_wake_turn_trigger(_CHILD_WAKE, _wake(), [_REQUEST])
    unnamed = lifecycle_wake_turn_trigger(_CHILD_WAKE, _wake(), [], "目" * 700)
    named_facts, unnamed_facts = json.loads(named.event_facts), json.loads(unnamed.event_facts)

    # 原任务在会话历史里时事件只引用编号；不在历史里时附一段有界原文，首句也不再声称它在历史里。
    assert "origin_task" not in named_facts and named_facts["origin_request_ids"] == [_REQUEST]
    assert unnamed_facts["origin_request_ids"] == [] and unnamed_facts["origin_task"].endswith("…（已截断）")
    assert (named.origin_task_attached, unnamed.origin_task_attached) == (False, True)
    assert current_turn_text("", named).splitlines()[1] == HOST_EVENT_FIRST_LINE
    assert current_turn_text("", unnamed).splitlines()[1] == HOST_EVENT_FIRST_LINE_ORIGIN_TASK


@pytest.mark.parametrize("goal_status", ["active", "complete"])
@pytest.mark.parametrize("ordinary_input", ["none", "present"])
def test_goal_task_origin_is_not_reported_as_a_history_request(tmp_path, goal_status, ordinary_input) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner", "channel": "tui", "channel_conversation_id": "goal"})
    task_id, objective = "durable-task-with-no-user-request", "完成知识库整理并整合子代理成果"
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": objective, "status": "active"})
    goal = store.goals.create({"thread_id": thread.thread_id, "task_id": task_id, "objective": objective})
    if goal_status == "complete":
        store.goals.update({"thread_id": thread.thread_id, "goal_id": goal.goal_id, "status": "complete"})
    metadata: dict = {"conversation_request_id": task_id}
    if ordinary_input == "present":
        metadata["events"] = [{"metadata": {"conversation_request_id": "real-user-request"}}]
        store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": "补充报告格式",
                               "metadata": {"conversation_request_id": "real-user-request"}})
    request = BackgroundRunRequest(thread_id=thread.thread_id, task_id=task_id, reason=_CHILD_WAKE,
                                   wake_signal={"metadata": metadata})

    context = _goal_runtime_context(SimpleNamespace(), store, request)
    _prompt, _injections, trigger = _background_model_inputs(
        request, task_objective=context.task_objective, wake_prompt="唤醒说明",
        goal_origin=(context.durable_goal_task_id, context.durable_goal_objective),
    )

    # Goal 的持久任务编号是任务来源，没有历史用户消息：不进 origin_request_ids，目标原文走 origin_task。
    history_requests = ["real-user-request"] if ordinary_input == "present" else []
    assert list(trigger.origin_request_ids) == history_requests
    facts = json.loads(trigger.event_facts)
    assert facts["origin_request_ids"] == history_requests and facts["origin_task"] == objective
    assert current_turn_text(context.task_objective, trigger).splitlines()[1] == HOST_EVENT_FIRST_LINE_ORIGIN_TASK


@pytest.mark.parametrize(("reason", "objective", "wake", "expected_kind"), [
    (_CHILD_WAKE, _OBJECTIVE, _wake(), "lifecycle_wake"),
    ("subagent_capability_request_open", _OBJECTIVE, _wake(), "lifecycle_wake"),
    (_CHILD_WAKE, "", _wake(), None),
    ("scheduled_progress_report", _OBJECTIVE, {}, None),
])
def test_model_inputs_pick_the_trigger_from_structured_facts(reason, objective, wake, expected_kind) -> None:
    request = BackgroundRunRequest(thread_id="thread-1", task_id=_TASK, reason=reason, wake_signal=wake)

    prompt, injections, trigger = _background_model_inputs(request, task_objective=objective, wake_prompt="唤醒说明")

    assert getattr(trigger, "kind", None) == expected_kind
    if expected_kind:
        assert (prompt, injections) == (_OBJECTIVE, ["[active-turn-continuation]\n唤醒说明"])
    else:
        assert (prompt, injections) == ("唤醒说明", [])


# 函数用途: 构造只填当前回合必需字段的循环参数。
def _loop_params(trigger: object) -> RuntimeLoopParams:
    return RuntimeLoopParams(
        user_prompt=_OBJECTIVE, root_user_prompt=_OBJECTIVE, memories=[], runtime_injections=[], routed_context=None,
        resume_context_section="", turn_trigger=trigger,
    )


def test_native_turn_opens_with_the_host_event_instead_of_a_second_user_task() -> None:
    trigger = _trigger()
    history = _native_initial_tool_ir_history(_loop_params(trigger), carried_handoff="交接摘要", carried_user_inputs=[])

    assert isinstance(history[0], RuntimeFactsTurn) and history[0].source == HOST_EVENT_FACTS_SOURCE
    assert history[0].text == current_turn_text(_OBJECTIVE, trigger)
    assert isinstance(history[1], CompactionSummary) and _current_turn_opener_count(history) == 1
    assert not any(isinstance(item, UserTurn) for item in history)
    assert _current_turn_opener_count([RuntimeFactsTurn("x", source="prompt.workspace")]) == 0

    ordinary = _native_initial_tool_ir_history(_loop_params(None), carried_handoff="", carried_user_inputs=[])
    assert ordinary == [UserTurn(f"# User Task\n{_OBJECTIVE}")]


def test_session_task_turn_opens_with_the_host_event_and_never_a_user_turn() -> None:
    """会话间派活片同样是宿主事件：派来的任务正文不能落在用户原话位置，来源标记也要可区分。"""
    trigger = session_task_trigger(
        TurnTrigger(
            kind=TURN_TRIGGER_SESSION_TASK,
            reason="session_task",
            event_facts='{"origin_thread_id": "thread-A", "task_id": "stask-1"}',
        )
    )
    assert trigger is not None

    history = _native_initial_tool_ir_history(_loop_params(trigger), carried_handoff="", carried_user_inputs=[])

    assert isinstance(history[0], RuntimeFactsTurn)
    assert history[0].source == SESSION_TASK_FACTS_SOURCE
    assert history[0].source != HOST_EVENT_FACTS_SOURCE
    assert SESSION_TASK_FIRST_LINE in history[0].text
    assert not any(isinstance(item, UserTurn) for item in history)
    assert _current_turn_opener_count(history) == 1


def test_session_task_turn_uses_its_own_fixed_recommendation() -> None:
    """派活回合用固定的推荐短名单与理由，不按任务文字做检索。"""
    trigger = TurnTrigger(kind=TURN_TRIGGER_SESSION_TASK, reason="session_task")
    names, reason = turn_trigger_recommendation(trigger) or ((), "")

    assert "get_session_task" in names and "send_session_message" in names
    assert "派活" in reason


@pytest.mark.parametrize("native", [True, False])
def test_prompt_uses_host_event_as_the_current_turn(tmp_path, native) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)
    trigger = _trigger()

    def render(turn_trigger):
        request = PromptBuildRequest(user_prompt=_OBJECTIVE, memories=[], tools=ToolSections(native_tool_use=native),
                                     turn_trigger=turn_trigger)
        return render_prepared_prompt(agent.prompts.prepare_render_input(request))

    wake_prompt, ordinary_prompt = render(trigger), render(None)
    assert current_turn_text(_OBJECTIVE, trigger) in wake_prompt and "# User Task" not in wake_prompt
    assert f"# User Task\n{_OBJECTIVE}" in ordinary_prompt and "# Host Event" not in ordinary_prompt
    if native:
        assert prompt_cache_layout(wake_prompt).canonical_user_turn == current_turn_text(_OBJECTIVE, trigger)


def _snapshot() -> ToolProtocolSnapshot:
    return ToolProtocolSnapshot("host-event-test", "native", ProviderToolCapability(
        provider="test", endpoint="local://host-event", model="test", stream=False, native_supported=True,
        evidence="canonical_test_fixture",
    ))


def test_wake_recommendations_are_a_fixed_shortlist_led_by_list_agents(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), execution_mode="local_unmanaged"),
        tmp_path / "service-root",
    )
    params = _run_params("thread-1", BackgroundRunRequest(thread_id="thread-1", task_id=_TASK, reason=_CHILD_WAKE), agent)

    def recommendations(allowed_tools, trigger):
        runtime_snapshot = agent.tools.runtime_snapshot(allowed_tools=allowed_tools, run_id=params.run_id)
        return _resolve_tool_sections(ToolSectionsRequest(
            agent=agent, user_prompt="请派一个子代理去读资料", allowed_tools=allowed_tools,
            runtime_snapshot=runtime_snapshot, protocol_snapshot=_snapshot(), turn_trigger=trigger,
        ))[1]

    wake = recommendations(params.allowed_tools, _trigger())
    names = [line[2:].split("：", 1)[0] for line in wake.splitlines() if line.startswith("- ")]
    assert names == ["list_agents", "read_file", "resolve_capability_requests", "send_guidance", "cancel_subagents"]
    without_list = [tool for tool in params.allowed_tools if tool != "list_agents"]
    assert recommendations(without_list, _trigger()).splitlines()[1].startswith("- read_file：")
    assert "create_subagents" in recommendations(params.allowed_tools, None)


def test_wake_slice_history_keeps_the_event_but_not_the_slice_injection() -> None:
    trigger = _trigger()
    history = [
        RuntimeFactsTurn(current_turn_text(_OBJECTIVE, trigger), source=HOST_EVENT_FACTS_SOURCE),
        RuntimeFactsTurn("# Workspace Context\n- 工作目录", source="prompt.workspace"),
        RuntimeFactsTurn("# Runtime Injection\n[background-main-agent-context]\n大段上下文", source="prompt.runtime_injection"),
        AssistantTurn(text="收到。"),
    ]

    def persisted(turn_trigger) -> str:
        params = SimpleNamespace(tool_ir_history=list(history), tool_protocol_snapshot=_snapshot(), turn_trigger=turn_trigger)
        return json.dumps(_completed_turn_native_messages(params, None), ensure_ascii=False)

    wake_slice = persisted(trigger)
    assert "# Host Event" in wake_slice and "# Workspace Context" in wake_slice and "收到。" in wake_slice
    assert "[background-main-agent-context]" not in wake_slice
    assert "[background-main-agent-context]" in persisted(None)


# ---- 脚本化假模型走真实后台链路 ----


def _native_probe(backend: object) -> ProviderToolCapability:
    return ProviderToolCapability(
        provider=str(getattr(backend, "name", "") or "host-event"), endpoint="local://host-event", model="",
        stream=False, native_supported=True, evidence="test_backend_declares_native_tools",
    )


def _texts(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    return [str(block.get("text") or "") for block in content or [] if isinstance(block, dict) and block.get("type") == "text"]


# 函数用途: 假模型的判断口径——最近一条 "# User Task" 出现在最后一条 assistant 之后，才算有新的用户任务。
def _fresh_user_task(messages: list[dict]) -> bool:
    tasks = [index for index, message in enumerate(messages)
             if message.get("role") == "user" and any(text.startswith("# User Task") for text in _texts(message))]
    assistants = [index for index, message in enumerate(messages) if message.get("role") == "assistant"]
    return bool(tasks) and tasks[-1] > (assistants[-1] if assistants else -1)


def _tool_result_text(messages: list[dict], call_id: str) -> str:
    return "\n".join(
        json.dumps(block, ensure_ascii=False) for message in messages for block in message.get("content") or []
        if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") == call_id
    )


# 类用途: 按"最新 User Task"行事的假模型；always_dispatch=True 时唤醒片一律派工，用来验证去重门。
class _LatestUserTaskBackend:
    name = "latest-user-task"

    def __init__(self, *, always_dispatch: bool) -> None:
        self.always_dispatch = always_dispatch
        self.requests: list[list[dict]] = []
        self.prompts: list[str] = []
        self.dispatched = 0

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        messages = [message for message in list(kwargs.get("messages") or []) if isinstance(message, dict)]
        self.requests.append(messages)
        self.prompts.append(str(prompt))
        if _tool_result_text(messages, "wake-create"):
            return ModelResponse(text="派工结果已收到，继续等待原子代理。", backend=self.name)
        if self.always_dispatch or _fresh_user_task(messages):
            self.dispatched += 1
            return ModelResponse(text="", backend=self.name, tool_use_blocks=[
                {"id": "wake-create", "name": "create_subagents", "input": {"goal": _CHILD_GOAL}},
            ])
        return ModelResponse(text="收到宿主事件：子代理被授权门拦下，等待用户确认。", backend=self.name)


# 函数用途: 按 T3 round1 的真实布局搭场景：前台成功派出一个子代理（记录在 owner 根索引），子代理随后进入 status（默认阻塞）。
def _blocked_child_scene(tmp_path, backend, status: str = "BLOCKED"):
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home"),
                                    orphan_supervision_interval_seconds=0), tmp_path)
    agent.backend = backend
    owner = agent.home_paths.owner_home_dir
    task_root = owner / "runs" / "2026-09-28" / "run-origin"
    (task_root / "work").mkdir(parents=True)
    (task_root / "work" / "run_workspace.json").write_text(json.dumps({
        "schema_version": "run_workspace.v1", "owner_home": str(owner), "task_root": str(task_root),
    }), encoding="utf-8")
    externalize_tool_output_record(ExternalizeToolOutputRequest(
        root=owner, tool="create_subagents", call_id="fg-create", output="created child", ok=True,
        request_id=_REQUEST, run_id=_REQUEST, task_id=_REQUEST, conversation_request_id=_REQUEST, min_chars=0,
        parameters={"goal": _CHILD_GOAL},
        result_envelope={"tool_operation": {"schema_version": "tool_operation.v1", "operation_id": "op-fg-create",
                                            "status": "succeeded", "action": "execute", "replayed": False}},
    ))
    child = agent.subagents.create_run(goal=_CHILD_GOAL, thought="", plan=["读取"], parent_id=_TASK, root_id=_TASK,
                                       attributes={"conversation_request_id": _REQUEST})
    agent.subagents.lifecycle.set_status(child.id, status)
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "internal",
                                          "channel_conversation_id": "thread-host-event", "channel_user_id": "user-1"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": _TASK, "goal": _OBJECTIVE, "status": "active",
                      "task_path": str(task_root)})
    store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": _OBJECTIVE,
                           "metadata": {"conversation_request_id": _REQUEST}})
    store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": "已派出子代理读取父项目资料，等待它的结果。",
                           "metadata": {"conversation_request_id": _REQUEST}})
    return agent, store, thread, child


def _run_wake(agent, store, thread, child) -> None:
    status = agent.subagents.load(child.id).status
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService())
    runtime.run_once({
        "thread_id": thread.thread_id, "task_id": _TASK, "reason": _CHILD_WAKE,
        "wake_signal": {**_wake(), "source_agent_id": child.id,
                        "metadata": {"task_id": child.id, "status": status,
                                     "turn_end_reason": "tool_failure_halt" if status == "BLOCKED" else "completed",
                                     "conversation_request_id": _REQUEST}},
    })


def test_scripted_model_following_the_latest_user_task_no_longer_redispatches(tmp_path) -> None:
    backend = _LatestUserTaskBackend(always_dispatch=False)
    agent, store, thread, child = _blocked_child_scene(tmp_path, backend)

    _run_wake(agent, store, thread, child)

    assert backend.dispatched == 0 and len(backend.requests) == 1
    request = backend.requests[0]
    assert not _fresh_user_task(request)
    assistants = [index for index, message in enumerate(request) if message.get("role") == "assistant"]
    current_turn = [text for message in request[assistants[-1] + 1:] for text in _texts(message)]
    assert current_turn[0].splitlines()[:2] == ["# Host Event", HOST_EVENT_FIRST_LINE]
    recommended = next(text for text in current_turn if text.startswith("# Recommended Tools"))
    assert recommended.splitlines()[1].startswith("- list_agents：")
    # 诊断/归档用的完整 prompt 与原生消息同口径：当前回合同样是宿主事件。
    assert f"# Host Event\n{HOST_EVENT_FIRST_LINE}" in backend.prompts[0] and "# User Task" not in backend.prompts[0]
    assert [run.id for run in agent.subagents.list_runs() if run.root_id == _TASK] == [child.id]
    # 唤醒片写进会话历史的是宿主事件，不是第二条用户任务，也不带本片的后台上下文注入。
    persisted = [row for row in store.messages.recent(thread.thread_id, limit=0)
                 if row.metadata.get("canonical_native_messages")]
    stored = json.dumps([row.metadata["canonical_native_messages"] for row in persisted], ensure_ascii=False)
    assert "# Host Event" in stored and "# User Task" not in stored
    assert "[background-main-agent-context]" not in stored


def test_scripted_model_that_always_dispatches_is_stopped_by_the_turn_dedupe(tmp_path) -> None:
    backend = _LatestUserTaskBackend(always_dispatch=True)
    agent, store, thread, child = _blocked_child_scene(tmp_path, backend)

    _run_wake(agent, store, thread, child)

    assert backend.dispatched == 1 and len(backend.requests) == 2
    assert "TOOL_ONE_SHOT_ALREADY_EXECUTED" in _tool_result_text(backend.requests[1], "wake-create")
    assert [run.id for run in agent.subagents.list_runs() if run.root_id == _TASK] == [child.id]


def test_goal_wake_through_the_real_chain_carries_the_goal_origin(tmp_path) -> None:
    backend = _LatestUserTaskBackend(always_dispatch=False)
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home"),
                                    orphan_supervision_interval_seconds=0), tmp_path)
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create({"canonical_user_id": "user-1", "channel": "internal",
                                          "channel_conversation_id": "thread-goal-origin", "channel_user_id": "user-1"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": _TASK, "goal": _OBJECTIVE, "status": "active"})
    store.goals.create({"thread_id": thread.thread_id, "task_id": _TASK, "objective": _OBJECTIVE})
    child = agent.subagents.create_run(goal=_CHILD_GOAL, thought="", plan=["读取"], parent_id=_TASK, root_id=_TASK)
    agent.subagents.lifecycle.set_status(child.id, "BLOCKED")

    # Goal 子代理的唤醒只带持久任务编号（没有对应的历史用户消息）。
    BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService()).run_once({
        "thread_id": thread.thread_id, "task_id": _TASK, "reason": _CHILD_WAKE,
        "wake_signal": {"wake_signal_id": "wake-goal", "root_task_id": _TASK, "source_agent_id": child.id,
                        "metadata": {"task_id": child.id, "status": "BLOCKED", "conversation_request_id": _TASK}},
    })

    host_event = backend.prompts[0][backend.prompts[0].index("# Host Event"):]
    assert host_event.splitlines()[1] == HOST_EVENT_FIRST_LINE_ORIGIN_TASK
    facts = json.loads(host_event.split("\n", 2)[2])
    assert facts["origin_request_ids"] == [] and facts["origin_task"] == _OBJECTIVE


def test_scripted_model_is_stopped_when_the_foreground_history_is_unreadable(tmp_path) -> None:
    backend = _LatestUserTaskBackend(always_dispatch=True)
    agent, store, thread, child = _blocked_child_scene(tmp_path, backend)
    # owner 根索引读不到（同名目录产生真实 OSError）：前台那次派工无法证明没做过，去重必须 fail-closed。
    index = agent.home_paths.owner_home_dir / "blobs" / "tool_outputs" / "index.jsonl"
    index.unlink()
    index.mkdir()

    _run_wake(agent, store, thread, child)

    assert backend.dispatched == 1 and len(backend.requests) == 2
    assert "TOOL_ONE_SHOT_HISTORY_INCOMPLETE" in _tool_result_text(backend.requests[1], "wake-create")
    assert [run.id for run in agent.subagents.list_runs() if run.root_id == _TASK] == [child.id]


# 类用途: 唤醒片先按原参数派工（被历史不完整门拦下），再按拒绝结果给出的唯一出口写 replacement_for_run_ids 接替已完成的子代理。
class _ReplaceAfterRejectionBackend:
    name = "replace-after-rejection"

    def __init__(self) -> None:
        self.requests: list[list[dict]] = []
        self.source_id = ""

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> ModelResponse:
        messages = [message for message in list(kwargs.get("messages") or []) if isinstance(message, dict)]
        self.requests.append(messages)
        if _tool_result_text(messages, "wake-replace"):
            return ModelResponse(text="已按接替关系另派子代理。", backend=self.name)
        if _tool_result_text(messages, "wake-create"):
            return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
                "id": "wake-replace", "name": "create_subagents",
                "input": {"goal": "接替已完成的子代理重新整理要点", "replacement_for_run_ids": [self.source_id]},
            }])
        return ModelResponse(text="", backend=self.name, tool_use_blocks=[
            {"id": "wake-create", "name": "create_subagents", "input": {"goal": _CHILD_GOAL}},
        ])


def test_history_incomplete_exit_can_supersede_a_done_child(tmp_path, monkeypatch) -> None:
    import agent_py_agent.agent.agent_core.orchestration.background.dispatch as background_dispatch

    # 只替换后台启动这一步：接替子代理照常创建和落账，但不在测试里真的跑它。
    monkeypatch.setattr(background_dispatch, "_start_background_dispatch",
                        lambda _agent, run_ids, **_kwargs: {"status": "started", "run_ids": list(run_ids)})
    backend = _ReplaceAfterRejectionBackend()
    agent, store, thread, child = _blocked_child_scene(tmp_path, backend, status="DONE")
    backend.source_id = child.id
    # owner 根索引读不到：普通重派被 fail-closed 拦下，但写明接替已完成子代理的派工必须照常放行并如实落账。
    index = agent.home_paths.owner_home_dir / "blobs" / "tool_outputs" / "index.jsonl"
    index.unlink()
    index.mkdir()

    _run_wake(agent, store, thread, child)

    assert len(backend.requests) == 3
    assert "TOOL_ONE_SHOT_HISTORY_INCOMPLETE" in _tool_result_text(backend.requests[1], "wake-create")
    replaced = _tool_result_text(backend.requests[2], "wake-replace")
    assert "superseded" in replaced and "not_persisted" not in replaced
    replacement = next(run for run in agent.subagents.list_runs() if run.id != child.id)
    source = agent.subagents.load(child.id)
    assert source.status == "DONE" and source.superseded_by == replacement.id
    assert replacement.attributes["replacement_for_run_ids"] == [child.id]
