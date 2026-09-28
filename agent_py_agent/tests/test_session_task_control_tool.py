"""get_session_task / cancel_session_task 的合同测试（无真实模型请求）。

覆盖：查询返回结构化状态、任务不存在返回 SESSION_TASK_NOT_FOUND、缺参数失败、
取消非终态任务生效并把通知排队、取消终态任务不改写（幂等返回当前状态）、
取消后状态确实是 cancelled。
"""

from __future__ import annotations

import json
import pathlib

from agent_py_agent.agent.agent_core.orchestration.tools.session_task_control import (
    CancelSessionTaskTool,
    GetSessionTaskTool,
)
from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.conversation import ConversationStore


class _Guidance:
    def __init__(self) -> None:
        self.calls = []

    def append_once(self, request, *, dedupe_key):
        from types import SimpleNamespace

        self.calls.append((request, dedupe_key))
        return SimpleNamespace(guidance_id=f"g-{len(self.calls)}")


class _Agent:
    def __init__(self, root: pathlib.Path) -> None:
        store = ConversationStore(root / "conv")
        store.guidance = _Guidance()
        self.conversation_store = store
        self._capability_config_runtime_snapshot = type("_S", (), {"config": CapabilityConfig()})()


def _agent(tmp_path):
    agent = _Agent(tmp_path)
    task = agent.conversation_store.session_tasks.create(
        sender_thread_id="A", target_thread_id="B", goal="做 X", body_guidance_id="g-body", now=1.0
    )
    return agent, task


def test_get_returns_structured_status(tmp_path) -> None:
    agent, task = _agent(tmp_path)
    outcome = GetSessionTaskTool(agent).execute({"task_id": task.task_id})
    assert outcome.ok
    payload = json.loads(outcome.output)
    assert payload["status"] == "queued"
    assert payload["body_guidance_id"] == "g-body"
    assert payload["goal"] == "做 X"


def test_get_missing_task_not_found(tmp_path) -> None:
    agent, _ = _agent(tmp_path)
    outcome = GetSessionTaskTool(agent).execute({"task_id": "stask-nonexistent"})
    assert not outcome.ok
    assert outcome.error_code == "SESSION_TASK_NOT_FOUND"


def test_get_missing_param_fails(tmp_path) -> None:
    agent, _ = _agent(tmp_path)
    outcome = GetSessionTaskTool(agent).execute({})
    assert not outcome.ok
    assert outcome.error_code == "TOOL_PARAMETER_REQUIRED"


def test_cancel_active_task_takes_effect_and_notifies(tmp_path) -> None:
    agent, task = _agent(tmp_path)
    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})
    assert outcome.ok
    payload = json.loads(outcome.output)
    assert payload["status"] == "cancelled"
    assert payload["notified_sender"] is True
    # store 里确实是 cancelled。
    assert agent.conversation_store.session_tasks.load(task.task_id).status == "cancelled"
    # 通知作为一条 message 排队回发送方。
    request, _key = agent.conversation_store.guidance.calls[0]
    assert request["target_id"] == "A"
    assert request["metadata"]["origin_kind"] == "session_task"


def test_cancel_terminal_task_is_noop(tmp_path) -> None:
    agent, task = _agent(tmp_path)
    store = agent.conversation_store.session_tasks
    store.advance(task.task_id, status="accepted", now=2.0)
    store.advance(task.task_id, status="done", summary="完成", now=3.0)
    outcome = CancelSessionTaskTool(agent).execute({"task_id": task.task_id})
    assert outcome.ok
    payload = json.loads(outcome.output)
    assert payload["status"] == "done"
    assert payload["already_terminal"] is True
    # 终态未被改写。
    assert store.load(task.task_id).status == "done"


def test_cancel_missing_task_not_found(tmp_path) -> None:
    agent, _ = _agent(tmp_path)
    outcome = CancelSessionTaskTool(agent).execute({"task_id": "stask-nonexistent"})
    assert not outcome.ok
    assert outcome.error_code == "SESSION_TASK_NOT_FOUND"
