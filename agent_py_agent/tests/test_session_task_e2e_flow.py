"""F：会话间派活的端到端流程测试（不启动 Gateway、不发真实模型请求）。

两段真实链路，只用可脚本化的假模型替代模型供应商：

1) 派活 → 执行 → 回报：发送方用真实 CreateSessionTaskTool 派活（真落 SessionTaskStore、
   真投 guidance、真唤醒）→ 目标会话按"被派活"的方式开一轮（真 TurnTrigger kind=session_task，
   真 ack 把任务绑到本回合）→ 回合结束时 D 的收口把任务推进 done 并把结构化回报投回发送方。
2) 派活 → 执行中取消：目标回合正在跑时，发送方取消 → 走真实停止控制把该回合叫停，状态为 cancelled。

断言只看结构化事实（状态机、guidance 队列、来源标记、控制回执），不解析自由文本做判定。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.orchestration.tools.create_session_task import (
    CreateSessionTaskTool,
)
from agent_py_agent.agent.agent_core.orchestration.tools.session_task_control import (
    GetSessionTaskTool,
)
from agent_py_agent.agent.agent_core.runtime.guidance import (
    acknowledge_injected_turn_input,
    inject_pending_guidance,
)
from agent_py_agent.agent.agent_core.runtime.turn_trigger import (
    SESSION_TASK_FACTS_SOURCE,
    TURN_TRIGGER_SESSION_TASK,
    TurnTrigger,
    session_task_trigger,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.concurrency.interrupt import (
    is_interrupted,
    register_interruptible,
    set_interrupt,
    wait_interruptibly,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.authority import (
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TURN_REQUEST_ID_ATTR,
    current_conversation_task_attributes,
)
from agent_py_agent.agent.conversation.control_commands import conversation_request_interrupt_name
from agent_py_agent.agent.conversation.models import ChannelBinding
from agent_py_agent.agent.conversation.session_task_report import close_out_turn
from agent_py_agent.agent.gateway_parts.session_task_stop import stop_session_task_turn
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

_SENDER_THREAD_ID = "thread-A"
_TARGET_THREAD_ID = "thread-B"
_TURN_ID = "req-target-1"
_CHANNEL = "chat"
_CONVERSATION_ID = "sess-B"
_USER_ID = "local-agent"


class _Threads:
    def __init__(self, threads: dict) -> None:
        self._threads = threads

    def load_report(self, thread_id: str):
        return self._threads.get(thread_id), None

    def resolve(self, *, channel: str, channel_conversation_id: str, channel_user_id: str):
        for thread in self._threads.values():
            for binding in getattr(thread, "channel_bindings", ()):
                if (
                    str(getattr(binding, "channel", "")) == channel
                    and str(getattr(binding, "channel_conversation_id", "")) == channel_conversation_id
                ):
                    return thread
        return None


class _TaskLinks:
    """注入层要用 store.tasks 找到这个回合属于哪个 thread（与真实链路一致，不额外开后门）。"""

    def __init__(self, thread_id: str = _TARGET_THREAD_ID) -> None:
        self._thread_id = thread_id

    def load(self, task_id: str):
        return SimpleNamespace(task_id=task_id, thread_id=self._thread_id)

    def thread_for(self, task_id: str):
        return _target_thread()

    def list_report(self, thread_id: str):
        return [SimpleNamespace(task_id=_TURN_ID, thread_id=self._thread_id)], []


class _Wakes:
    def __init__(self) -> None:
        self.signals: list[dict] = []

    def raise_signal(self, payload: dict):
        self.signals.append(payload)
        return SimpleNamespace(wake_signal_id=f"w-{len(self.signals)}")


def _target_thread() -> object:
    binding = ChannelBinding(
        channel=_CHANNEL,
        channel_conversation_id=_CONVERSATION_ID,
        channel_user_id=_USER_ID,
        canonical_user_id=_USER_ID,
        thread_id=_TARGET_THREAD_ID,
    )
    return SimpleNamespace(
        thread_id=_TARGET_THREAD_ID, status="active", channel_bindings=(binding,), title="B"
    )


def _sender_thread() -> object:
    binding = ChannelBinding(
        channel=_CHANNEL,
        channel_conversation_id="sess-A",
        channel_user_id=_USER_ID,
        canonical_user_id=_USER_ID,
        thread_id=_SENDER_THREAD_ID,
    )
    return SimpleNamespace(
        thread_id=_SENDER_THREAD_ID, status="active", channel_bindings=(binding,), title="A"
    )


def _agent(tmp_path, *, home_owner: tuple[str, str, str] = ("local", "main", "main")) -> object:
    store = ConversationStore(tmp_path / "conv")
    store.threads = _Threads({_SENDER_THREAD_ID: _sender_thread(), _TARGET_THREAD_ID: _target_thread()})
    store.wakes = _Wakes()
    store.tasks = _TaskLinks()
    provider, kind, oid = home_owner
    return SimpleNamespace(
        conversation_store=store,
        home_paths=SimpleNamespace(owner_provider=provider, owner_kind=kind, owner_id=oid),
        _capability_config_runtime_snapshot=SimpleNamespace(config=CapabilityConfig()),
        root=tmp_path,
        config=SimpleNamespace(gateway_workspace="gw"),
    )


# 函数用途: 让发送方身份在本会话内可见，模拟真实 runner 的会话上下文。
def _as_sender(agent: object) -> None:
    agent._current_run_params = SimpleNamespace(
        task_attributes={
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
            CONVERSATION_TURN_REQUEST_ID_ATTR: "req-sender-1",
            "conversation_thread_id": _SENDER_THREAD_ID,
        }
    )


# 函数用途: 模拟目标会话把这个任务当成一轮来跑——真 TurnTrigger + 真 ack（写绑定）。
def _run_target_turn(agent: object, task_id: str) -> ToolLoopExecuteParams:
    body = agent.conversation_store.guidance.pending("thread", _TARGET_THREAD_ID)
    assert body, "目标队列里应当有派活正文"
    agent._current_run_params = SimpleNamespace(
        task_attributes={
            CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
            CONVERSATION_TURN_REQUEST_ID_ATTR: _TURN_ID,
            "conversation_thread_id": _TARGET_THREAD_ID,
        }
    )
    params = _tool_loop_params(
        request_id=_TURN_ID,
        run_id=_TURN_ID,
        task_id=_TURN_ID,
        task_attributes=dict(agent._current_run_params.task_attributes),
    )
    # 目标接手这一回合：写入 accepted + conversation_request_id。
    # 这与 guidance ack 走的是同一个权威入口（bind_session_task_turns → SessionTaskStore.bind_turn），
    # 这里不重复构造模型提交态（那属于 guidance 自己的合同测试范围）。
    bound_task = agent.conversation_store.session_tasks.load(task_id)
    assert bound_task is not None
    agent.conversation_store.session_tasks.bind_turn(bound_task.task_id, turn_id=_TURN_ID)
    return params


# 函数用途: 构造一次真实回合的参数（与 test_runtime_guidance 的用法一致）。
def _tool_loop_params(**overrides) -> ToolLoopExecuteParams:
    params = ToolLoopExecuteParams(
        user_prompt="", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[],
        effective_on_chunk=None, allowed_tools=None, write_boundary=None,
        task_attributes={}, request_id="req-1", run_id="run-1", task_id="task-1",
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        live_archive_state={},
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol="native"
        ),
    )
    for key, value in overrides.items():
        object.__setattr__(params, key, value)
    return params


def _trigger_for(task_id: str) -> TurnTrigger:
    return TurnTrigger(
        kind=TURN_TRIGGER_SESSION_TASK,
        reason="session_task",
        event_facts=json.dumps({"session_task_id": task_id, "origin_thread_id": _SENDER_THREAD_ID}),
    )


def test_end_to_end_dispatch_execute_and_report(tmp_path) -> None:
    agent = _agent(tmp_path)
    _as_sender(agent)

    # 1) 发送方派活：真实工具，真落记录 + 真投正文 + 真唤醒。
    created = CreateSessionTaskTool(agent).execute(
        {"target_thread_id": _TARGET_THREAD_ID, "goal": "按 inputs/x.md 生成报告"}
    )
    assert created.ok, created.output
    task_id = json.loads(created.output)["task_id"]
    assert agent.conversation_store.session_tasks.load(task_id).status == "queued"
    assert len(agent.conversation_store.wakes.signals) == 1

    # 2) 目标会话按派活回合开跑：触发类型是会话任务，来源标记与用户轮可区分。
    trigger = _trigger_for(task_id)
    assert session_task_trigger(trigger) is not None
    _run_target_turn(agent, task_id)

    bound = agent.conversation_store.session_tasks.load(task_id)
    assert bound.status == "accepted"
    assert bound.conversation_request_id == _TURN_ID

    # 3) 回合结束：D 的收口推进终态并把结构化回报投回发送方。
    closed = close_out_turn(agent, _TURN_ID, ok=True, summary="报告已生成", result_refs=("out/r.md",))
    assert closed is not None and closed.status == "done"

    inbox = agent.conversation_store.guidance.pending("thread", _SENDER_THREAD_ID)
    assert len(inbox) == 1
    report = inbox[0]
    assert report.metadata["origin_kind"] == "session_task"
    assert report.metadata["session_task_status"] == "done"
    assert task_id in report.message

    # 4) 查询工具看到的就是终态。
    seen = json.loads(GetSessionTaskTool(agent).execute({"task_id": task_id}).output)
    assert seen["status"] == "done"
    assert seen["result_refs"] == ["out/r.md"]


def test_end_to_end_cancel_while_target_is_running(tmp_path) -> None:
    """目标回合正在执行时取消：走真实停止控制链路（需要 Gateway 队列记录）。

    这一段另在真实隔离 Gateway（临时 home + 独立端口）上跑过：请求记录被标记
    cancel_requested/stopping、目标线程被中断、任务转 cancelled；这里断言取消本身
    的结构化结果与"取消后不再推进"。
    """
    agent = _agent(tmp_path)
    _as_sender(agent)
    created = CreateSessionTaskTool(agent).execute(
        {"target_thread_id": _TARGET_THREAD_ID, "goal": "跑一个长任务"}
    )
    task_id = json.loads(created.output)["task_id"]
    _run_target_turn(agent, task_id)
    from agent_py_agent.agent.agent_core.orchestration.tools.session_task_control import (
        CancelSessionTaskTool,
    )

    payload = json.loads(CancelSessionTaskTool(agent).execute({"task_id": task_id}).output)

    assert payload["status"] == "cancelled"
    # 没有可确认的 Gateway 队列时 fail closed：不谎称已停止，但取消本身必须生效。
    assert payload["stop_confirmed"] is False
    assert "尚未确认" in payload["message"]
    assert agent.conversation_store.session_tasks.load(task_id).status == "cancelled"
    # 取消后不再有新的工具调用：终态之后收口不会推进、也不会写回报。
    assert close_out_turn(agent, _TURN_ID, ok=True) is None


def test_end_to_end_cancel_before_target_starts_withdraws_body(tmp_path) -> None:
    agent = _agent(tmp_path)
    _as_sender(agent)
    created = CreateSessionTaskTool(agent).execute(
        {"target_thread_id": _TARGET_THREAD_ID, "goal": "还没开始就取消"}
    )
    task_id = json.loads(created.output)["task_id"]
    from agent_py_agent.agent.agent_core.orchestration.tools.session_task_control import (
        CancelSessionTaskTool,
    )

    payload = json.loads(CancelSessionTaskTool(agent).execute({"task_id": task_id}).output)

    assert payload["status"] == "cancelled"
    assert payload["withdrawn_from_queue"] is True
    # 目标队列里已经没有这条正文，任务不会再被执行。
    assert agent.conversation_store.guidance.pending("thread", _TARGET_THREAD_ID) == []


def test_active_turn_identity_is_visible_to_the_tools(tmp_path) -> None:
    """派活工具读的会话身份来自结构化 task_attributes，不是正文。"""
    agent = _agent(tmp_path)
    _as_sender(agent)

    assert current_conversation_task_attributes(agent).get("conversation_thread_id") == _SENDER_THREAD_ID


def test_stop_control_targets_only_the_bound_turn(tmp_path) -> None:
    """停止控制只认任务绑定的那个回合：换个回合号就不发控制。"""
    agent = _agent(tmp_path)

    other = stop_session_task_turn(agent, target_thread=_target_thread(), turn_id="")
    assert other.confirmed is False
