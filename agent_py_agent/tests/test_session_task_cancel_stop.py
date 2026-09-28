"""cancel_session_task 的"真的叫停目标"合同测试（无真实模型请求）。

覆盖 dev 12:08 的要求：目标正在执行这个任务时，取消要向它绑定的那个回合发送 /stop 同款结构化停止控制
（请求记录被标记 + 该回合的注册线程被中断），不能停掉目标会话里别的工作；任务还没开始执行时只改状态
并从目标队列撤掉正文，保证不会再被执行；停止未确认时回执不得声称"已停止"；已是终态时不改状态、
也不发停止控制。另覆盖把任务绑到目标回合的绑定函数（accepted + request id）。
"""

from __future__ import annotations

import json
import pathlib
import threading
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.tools import session_task_control
from agent_py_agent.agent.agent_core.orchestration.tools.session_task_control import (
    CancelSessionTaskTool,
)
from agent_py_agent.agent.concurrency.interrupt import (
    register_interruptible,
    set_interrupt,
    wait_interruptibly,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.control_commands import conversation_request_interrupt_name
from agent_py_agent.agent.conversation.models import ChannelBinding
from agent_py_agent.agent.conversation.session_tasks import bind_session_task_turns
from agent_py_agent.agent.gateway_parts.session_task_stop import (
    SessionTaskStopOutcome,
    stop_session_task_turn,
)

_SENDER_THREAD_ID = "thread-A"
_TARGET_THREAD_ID = "thread-B"
_TURN_ID = "req-target-1"
_CHANNEL = "chat"
_CONVERSATION_ID = "sess-1"
_USER_ID = "local-agent"


class _Threads:
    """只提供取消路径需要的那一个目标会话读取；别的 thread 视为不存在。"""

    def __init__(self, thread: object) -> None:
        self._thread = thread

    def load_report(self, thread_id: str):
        if thread_id == _TARGET_THREAD_ID:
            return self._thread, None
        return None, None


def _target_thread() -> object:
    binding = ChannelBinding(
        channel=_CHANNEL,
        channel_conversation_id=_CONVERSATION_ID,
        channel_user_id=_USER_ID,
        canonical_user_id=_USER_ID,
        thread_id=_TARGET_THREAD_ID,
    )
    return SimpleNamespace(thread_id=_TARGET_THREAD_ID, channel_bindings=(binding,))


def _agent(tmp_path: pathlib.Path) -> object:
    store = ConversationStore(tmp_path / "conv")
    store.threads = _Threads(_target_thread())
    return SimpleNamespace(
        conversation_store=store,
        root=tmp_path,
        config=SimpleNamespace(gateway_workspace="gw"),
    )


def _write_processing_request(agent: object, *, request_id: str = _TURN_ID) -> pathlib.Path:
    processing = agent.root / agent.config.gateway_workspace / "requests" / "processing"
    processing.mkdir(parents=True, exist_ok=True)
    path = processing / f"{request_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": request_id,
                "user_id": _USER_ID,
                "conversation": {
                    "channel": _CHANNEL,
                    "channel_conversation_id": _CONVERSATION_ID,
                    "channel_user_id": _USER_ID,
                    "canonical_user_id": _USER_ID,
                },
                "turn_phase": "open",
                "status": "processing",
                "execution_attempt_id": "att-1",
            }
        ),
        encoding="utf-8",
    )
    return path


def _queued_task(agent: object, *, turn_id: str = "") -> object:
    """建一条派活记录：正文按真实路径投进目标 guidance 队列，可选地绑定到目标回合。"""
    store = agent.conversation_store
    dedupe_key = f"session_task:{_SENDER_THREAD_ID}->{_TARGET_THREAD_ID}:做 X"
    body_key = f"body:{dedupe_key}"
    body = store.guidance.append_once(
        {
            "target_type": "thread",
            "target_id": _TARGET_THREAD_ID,
            "message": "做 X",
            "sender": _SENDER_THREAD_ID,
            "priority": "normal",
            "delivery": "next_turn",
            "metadata": {"origin_kind": "session_task", "origin_thread_id": _SENDER_THREAD_ID},
        },
        dedupe_key=body_key,
    )
    task = store.session_tasks.create(
        sender_thread_id=_SENDER_THREAD_ID,
        target_thread_id=_TARGET_THREAD_ID,
        goal="做 X",
        body_guidance_id=body.guidance_id,
        body_dedupe_key=body_key,
        dedupe_key=dedupe_key,
    )
    if turn_id:
        store.session_tasks.bind_turn(task.task_id, turn_id=turn_id)
    return store.session_tasks.load(task.task_id)


def test_cancel_queued_task_withdraws_body_from_target_queue(tmp_path) -> None:
    agent = _agent(tmp_path)
    task = _queued_task(agent)

    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})

    payload = json.loads(outcome.output)
    assert outcome.ok
    assert payload["status"] == "cancelled"
    assert payload["withdrawn_from_queue"] is True
    assert "不会再被执行" in payload["message"]
    # 撤销是真的：注入层已经取不到这条正文，回执为 rejected。
    assert agent.conversation_store.guidance.pending("thread", _TARGET_THREAD_ID) == []
    receipt = agent.conversation_store.guidance.receipt(task.body_dedupe_key)
    assert receipt is not None and receipt.status == "rejected"


def test_cancel_running_task_sends_stop_to_bound_turn_only(tmp_path) -> None:
    agent = _agent(tmp_path)
    request_path = _write_processing_request(agent)
    task = _queued_task(agent, turn_id=_TURN_ID)
    result: dict[str, object] = {}
    interrupted: dict[str, bool] = {}

    def _run() -> None:
        result["outcome"] = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})

    def _target_turn() -> None:
        # 目标会话的回合线程按 request id 注册；被中断的线程会在阻塞等待上立刻醒来，
        # 这就是"这一轮收到了结构化停止控制"的可观察证据。
        with register_interruptible(conversation_request_interrupt_name(_TURN_ID)):
            try:
                worker_started.wait(timeout=30)
                interrupted["target"] = bool(wait_interruptibly(20))
            except InterruptedError:
                interrupted["target"] = True

    started = threading.Event()
    worker_started = started
    worker = threading.Thread(target=_run, name="session-task-cancel")
    turn = threading.Thread(target=_target_turn, name="session-task-target-turn")
    try:
        turn.start()
        worker.start()
        started.set()
        turn.join(timeout=30)
        worker.join(timeout=30)
        assert not worker.is_alive() and not turn.is_alive()
        payload = json.loads(result["outcome"].output)  # type: ignore[union-attr]
        assert payload["status"] == "cancelled"
        # 回执带上控制服务的原话，且任务确实绑到了这个回合。
        assert payload["stop_message"]
        # 这个回合收到了结构化停止控制：请求被标记 cancel_requested，线程被中断。
        stored = json.loads(request_path.read_text(encoding="utf-8"))
        assert stored["cancel_requested"] is True
        assert stored["control_status"] == "stopping"
        assert interrupted["target"] is True
    finally:
        set_interrupt(False)


def test_cancel_unconfirmed_stop_never_claims_stopped(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path)
    task = _queued_task(agent, turn_id=_TURN_ID)
    monkeypatch.setattr(
        session_task_control,
        "_stop_target_turn",
        lambda *args, **kwargs: SessionTaskStopOutcome(False, "目标会话没有可用的渠道身份，未发送停止控制。"),
    )

    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})

    payload = json.loads(outcome.output)
    assert payload["status"] == "cancelled"
    assert payload["stop_confirmed"] is False
    assert "尚未确认" in payload["message"]
    assert "已停止" not in payload["message"]


def test_cancel_terminal_task_does_not_send_stop(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path)
    task = _queued_task(agent, turn_id=_TURN_ID)
    store = agent.conversation_store.session_tasks
    store.advance(task.task_id, status="done")
    called: list[str] = []
    monkeypatch.setattr(
        session_task_control,
        "_stop_target_turn",
        lambda *args, **kwargs: called.append("stop") or SessionTaskStopOutcome(True, "不该被调用"),
    )

    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})

    payload = json.loads(outcome.output)
    assert payload["already_terminal"] is True
    assert "stop_confirmed" not in payload
    assert called == []
    assert store.load(task.task_id).status == "done"


def test_bind_session_task_turns_records_target_turn_once(tmp_path) -> None:
    agent = _agent(tmp_path)
    task = _queued_task(agent)
    store = agent.conversation_store
    entries = store.guidance.pending("thread", _TARGET_THREAD_ID)
    assert len(entries) == 1

    assert bind_session_task_turns(store, entries, _TURN_ID) == 1
    bound = store.session_tasks.load(task.task_id)
    assert bound.status == "accepted"
    assert bound.conversation_request_id == _TURN_ID

    # 同一回合重复确认幂等；终态后不再接受新的回合绑定。
    assert bind_session_task_turns(store, entries, _TURN_ID) == 0
    store.session_tasks.advance(task.task_id, status="done")
    assert bind_session_task_turns(store, entries, "req-other") == 0
    assert store.session_tasks.load(task.task_id).conversation_request_id == _TURN_ID


def test_stop_session_task_turn_fails_closed_without_structured_facts(tmp_path) -> None:
    agent = _agent(tmp_path)

    # 没有回合号：不发控制。
    no_turn = stop_session_task_turn(agent, target_thread=_target_thread(), turn_id="")
    assert no_turn.confirmed is False

    # 目标会话没有渠道身份：不发控制。
    no_binding = stop_session_task_turn(
        agent,
        target_thread=SimpleNamespace(thread_id=_TARGET_THREAD_ID, channel_bindings=()),
        turn_id=_TURN_ID,
    )
    assert no_binding.confirmed is False
    assert "渠道身份" in no_binding.message

    # 拿不到 Gateway 队列：不发控制，并给出未确认错误码。
    without_gateway = SimpleNamespace(conversation_store=agent.conversation_store)
    missing = stop_session_task_turn(
        without_gateway, target_thread=_target_thread(), turn_id=_TURN_ID
    )
    assert missing.confirmed is False
    assert missing.error_code == "SESSION_TASK_STOP_UNCONFIRMED"
